# ----------------------------------------------------------------------------
# Copyright (c) 2016-2017, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import itertools
import os.path
import sqlite3
import uuid

import pandas as pd


class Metadata:
    def __init__(self, dataframe):
        # Not using DataFrame.empty because empty columns are allowed.
        if dataframe.index.empty:
            raise ValueError("Metadata is empty, there must be at least one "
                             "ID associated with it.")

        # `/` and `\0` aren't permitted because they are invalid filename
        # characters on *nix filesystems. The remaining values aren't permitted
        # because they *could* be misinterpreted by a shell (e.g. `*`, `|`).
        illegal_chars = ['/', '\0', '\\', '*', '<', '>', '?', '|', '$']
        chars_for_msg = ", ".join("%r" % i for i in illegal_chars)
        illegal_chars = set(illegal_chars)

        for (axis, label) in [(dataframe.columns, 'category label'),
                              (dataframe.index, 'index')]:
            # First check the axis dtype
            if axis.dtype_str not in ['object', 'str']:
                msg = "Non-string Metadata %s values detected" % label
                raise ValueError(invalid_metadata_template % msg)

            # Then check for invalid characters along axis
            for value in axis:
                if illegal_chars & set(value):
                    msg = "Invalid characters (e.g. %s) detected in " \
                          "metadata %s: %r" % (chars_for_msg, label, value)
                    raise ValueError(invalid_metadata_template % msg)

            # Finally, ensure unique values along axis
            if len(axis) != len(set(axis)):
                msg = "Duplicate Metadata %s values detected" % label
                raise ValueError(invalid_metadata_template % msg)

        self._dataframe = dataframe
        self._artifacts = []

    def __repr__(self):
        return repr(self._dataframe)

    def _repr_html_(self):
        return self._dataframe._repr_html_()

    def __eq__(self, other):
        return (
            isinstance(other, self.__class__) and
            self._artifacts == other._artifacts and
            self._dataframe.equals(other._dataframe)
        )

    def __ne__(self, other):
        return not (self == other)

    @property
    def artifacts(self):
        return self._artifacts

    @classmethod
    def from_artifact(cls, artifact):
        """
        Parameters
        ----------
        artifact: qiime2.Artifact
           Loaded artifact object.

        Returns
        -------
        qiime2.Metadata
        """
        if not artifact.has_metadata():
            raise ValueError('Artifact has no metadata.')

        md = artifact.view(cls)
        md._artifacts.append(artifact)
        return md

    @classmethod
    def load(cls, path):
        if not os.path.exists(path):
            raise OSError(
                "Metadata file %s doesn't exist or isn't accessible (e.g., "
                "due to incompatible file permissions)." % path)

        read_csv_kwargs = {}
        with open(path, 'r') as fh:
            peek = fh.readline().rstrip('\n')
            if peek.startswith('#SampleID'):
                header = peek.split('\t')
                read_csv_kwargs = {'header': None, 'names': header}

        try:
            df = pd.read_csv(path, sep='\t', dtype=object, comment='#',
                             skip_blank_lines=True, **read_csv_kwargs)
            df.set_index(df.columns[0], drop=True, append=False, inplace=True)
        except (pd.io.common.CParserError, KeyError):
            msg = 'Metadata file format is invalid for file %s' % path
            raise ValueError(invalid_metadata_template % msg)
        return cls(df)

    def merge(self, *others):
        """Merge this ``Metadata`` object with other ``Metadata`` objects.

        Returns a new ``Metadata`` object containing the merged contents of
        this ``Metadata`` object and `others`. The merge is not in-place and
        will always return a **new** merged ``Metadata`` object.

        The merge will include only those IDs that are shared across **all**
        ``Metadata`` objects being merged (i.e. the merge is an *inner join*).

        Each metadata category (i.e. column) being merged must be unique;
        merging metadata with overlapping categories will result in an error.

        Parameters
        ----------
        others : tuple
            Zero or more ``Metadata`` objects to merge with this ``Metadata``
            object.

        Returns
        -------
        Metadata
            New object containing merged metadata. The merged IDs will be in
            the same relative order as the IDs in this ``Metadata`` object
            after performing the inner join. The merged category order
            (i.e. column order) will match the category order of ``Metadata``
            objects being merged from left to right.

        Notes
        -----
        The merged metadata object tracks all source artifacts that it was
        built from to preserve provenance (i.e. the ``.artifacts`` property
        on all ``Metadata`` objects is merged).

        """
        dfs = []
        columns = []
        artifacts = []
        for md in itertools.chain([self], others):
            df = md._dataframe
            dfs.append(df)
            columns.extend(df.columns.tolist())
            artifacts.extend(md.artifacts)

        columns = pd.Index(columns)
        if columns.has_duplicates:
            raise ValueError(
                "Cannot merge metadata with overlapping categories "
                "(i.e. overlapping columns). The following categories "
                "overlap: %s" %
                ', '.join([repr(e) for e in columns.get_duplicates()]))

        merged_df = dfs[0].join(dfs[1:], how='inner')

        # Not using DataFrame.empty because empty columns are allowed.
        if merged_df.index.empty:
            raise ValueError(
                "Cannot merge because there are no IDs shared across metadata "
                "objects.")

        merged_md = self.__class__(merged_df)
        merged_md._artifacts = artifacts
        return merged_md

    def get_category(self, *names):
        if len(names) != 1:
            # TODO: Make this work with multiple columns as a single series
            raise NotImplementedError("Extracting multiple columns is not yet"
                                      " supported.")
        try:
            result = MetadataCategory(self._dataframe[names[0]])
        except KeyError:
            raise KeyError(
                '%s is not a category in metadata file. Available '
                'categories are %s.' %
                (names[0], ', '.join(self._dataframe.columns)))
        else:
            result._artifacts.extend(self.artifacts)
        return result

    def to_dataframe(self):
        return self._dataframe.copy()

    def ids(self, where=None):
        """Retrieve IDs matching search criteria.

        Parameters
        ----------
        where : str, optional
            SQLite WHERE clause specifying criteria IDs must meet to be
            included in the results. All IDs are included by default.

        Returns
        -------
        set
            IDs matching search criteria specified in `where`.

        """
        if where is None:
            return set(self._dataframe.index)

        conn = sqlite3.connect(':memory:')
        conn.row_factory = lambda cursor, row: row[0]

        # If the index isn't named, generate a unique random column name to
        # store it under in the SQL table. If we don't supply a column name for
        # the unnamed index, pandas will choose the name 'index', and if that
        # name conflicts with existing columns, the name will be 'level_0',
        # 'level_1', etc. Instead of trying to guess what pandas named the
        # index column (since this isn't documented behavior), explicitly
        # generate an index column name.
        index_column = self._dataframe.index.name
        if index_column is None:
            index_column = self._generate_column_name()
        self._dataframe.to_sql('metadata', conn, index=True,
                               index_label=index_column)

        c = conn.cursor()

        # In general we wouldn't want to format our query in this way because
        # it leaves us open to sql injection, but it seems acceptable here for
        # a few reasons:
        # 1) This is a throw-away database which we're just creating to have
        #    access to the query language, so any malicious behavior wouldn't
        #    impact any data that isn't temporary
        # 2) The substitution syntax recommended in the docs doesn't allow
        #    us to specify complex `where` statements, which is what we need to
        #    do here. For example, we need to specify things like:
        #        WHERE Subject='subject-1' AND SampleType='gut'
        #    but their qmark/named-style syntaxes only supports substition of
        #    variables, such as:
        #        WHERE Subject=?
        # 3) sqlite3.Cursor.execute will only execute a single statement so
        #    inserting multiple statements
        #    (e.g., "Subject='subject-1'; DROP...") will result in an
        #    OperationalError being raised.
        query = ('SELECT "{0}" FROM metadata WHERE {1} GROUP BY "{0}" '
                 'ORDER BY "{0}";'.format(index_column, where))

        try:
            c.execute(query)
        except sqlite3.OperationalError:
            conn.close()
            raise ValueError("Selection of IDs failed with query:\n %s"
                             % query)

        ids = set(c.fetchall())
        conn.close()
        return ids

    def _generate_column_name(self):
        """Generate column name that doesn't clash with current columns."""
        while True:
            name = str(uuid.uuid4())
            if name not in self._dataframe.columns:
                return name


class MetadataCategory:
    def __init__(self, series):
        self._series = series
        self._artifacts = []

    def __repr__(self):
        return repr(self._series)

    @classmethod
    def load(cls, path, category):
        return Metadata.load(path).get_category(category)

    def to_series(self):
        return self._series.copy()

    @property
    def artifacts(self):
        return self._artifacts

    @classmethod
    def from_artifact(cls, artifact, category):
        """
        Parameters
        ----------
        artifact: qiime2.Artifact
           Loaded artifact object.

        Returns
        -------
        qiime.Metadata
        """
        return Metadata.from_artifact(artifact).get_category(category)


invalid_metadata_template = "%s. There may be more errors present in this " \
    "metadata. Currently only QIIME 1 sample/feature metadata mapping files " \
    "are officially supported. Sample metadata files can be validated using " \
    "Keemei: http://keemei.qiime.org."
