import collections
import tempfile
import sys
import os
import sqlite3
import constants
import version
import parser
import bins
import helpers
import feature
import interface
import iterators

import logging

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(ch)


class _DBCreator(object):
    def __init__(self, data, dbfn, force=False, verbose=True, id_spec=None,
                 merge_strategy='merge', checklines=10, transform=None,
                 force_dialect_check=False, from_string=False, dialect=None):
        """
        Base class for _GFFDBCreator and _GTFDBCreator; see create_db()
        function for docs
        """
        self.merge_strategy = merge_strategy

        self._autoincrements = collections.defaultdict(int)
        if force:
            if os.path.exists(dbfn):
                os.unlink(dbfn)
        self.dbfn = dbfn
        self.id_spec = id_spec
        conn = sqlite3.connect(dbfn)
        self.conn = conn
        #self.conn.text_factory = sqlite3.OptimizedUnicode
        self.conn.text_factory = str
        self.conn.row_factory = sqlite3.Row
        self._data = data

        self.verbose = verbose
        self._orig_logger_level = logger.level
        if not self.verbose:
            logger.setLevel(logging.ERROR)

        self.iterator = iterators.DataIterator(
            data=data, checklines=checklines, transform=transform,
            force_dialect_check=force_dialect_check, from_string=from_string,
            dialect=dialect
        )

    def _increment_featuretype_autoid(self, key):
        self._autoincrements[key] += 1
        return '%s_%s' % (key, self._autoincrements[key])

    def _id_handler(self, f):
        """
        Given a Feature from self.iterator, figure out what the ID should be.

        `_autoincrement_key` is which field to use that will be
        auto-incremented.  Typically this will be "feature" (for exon_1,
        exon_2, etc), but another useful one is "id", which is is used for
        duplicate IDs.
        """

        # If id_spec is a string, convert to iterable for later
        if isinstance(self.id_spec, basestring):
            id_key = [self.id_spec]

        elif hasattr(self.id_spec, '__call__'):
            id_key = [self.id_spec]

        # If dict, then assume it's a feature -> attribute mapping, e.g.,
        # {'gene': 'gene_id'} for GTF
        elif isinstance(self.id_spec, dict):
            try:
                id_key = self.id_spec[f.featuretype]
                if isinstance(id_key, basestring):
                    id_key = [id_key]

            # Otherwise, use default auto-increment.
            except KeyError:
                return self._increment_featuretype_autoid(f.featuretype)

        # Otherwise assume it's an iterable.
        else:
            id_key = self.id_spec

        # Then try them in order, returning the first one that works:
        for k in id_key:

            if hasattr(k, '__call__'):
                _id = k(f)
                if _id:
                    if _id.startswith('autoincrement:'):
                        return self._increment_featuretype_autoid(_id[14:])
                    return _id

            # use GFF fields rather than attributes for cases like :seqid: or
            # :strand:
            if (len(k) > 3) and (k[0] == ':') and (k[-1] == ':'):
                # No [0] here -- only attributes key/vals are forced into
                # lists, not standard GFF fields.
                return getattr(f, k[1:-1])
            else:
                v = f.attributes[k]
            if len(v) == 0:
                del f.attributes[k]
            else:
                return v[0]

        # If we get here, then default autoincrement
        return self._increment_featuretype_autoid(f.featuretype)

    def _get_feature(self, ID):
        c = self.conn.cursor()
        results = c.execute(
            constants._SELECT + ' WHERE id = ?', (ID,)).fetchone()
        return feature.Feature(dialect=self.iterator.dialect, **results)

    def _do_merge(self, f):
        """
        Different merge strategies upon name conflicts.

        "error":
            raise error

        "warning"
            show warning

        "merge":
            combine old and new attributes -- but only if everything else
            matches; otherwise error.  This can be slow, but is thorough.

        "create_unique":
            Autoincrement based on the ID

        "replace":
            Replaces existing feature with `f`.
        """
        if self.merge_strategy == 'error':
            raise ValueError("Duplicate ID {0.id}".format(f))
        if self.merge_strategy == 'warning':
            logger.warning(
                "Duplicate lines in file for id '{0.id}'; "
                "ignoring all but the first".format(f))
            return None
        elif self.merge_strategy == 'replace':
            return f
        elif self.merge_strategy == 'merge':
            # retrieve the existing row
            existing_feature = self._get_feature(f.id)

            # does everything besides attributes and extra match?
            for k in constants._gffkeys[:-1]:
                # Note str() here: `existing_d` came from the db (so start/end
                # are integers) but `d` came from the file, so they are still
                # strings.
                assert getattr(existing_feature, k) == getattr(f, k), (
                    "Same ID, but differing info for %s field. "
                    "Line %s: \n%s" % (
                        f.id,
                        self.iterator.current_item_number,
                        self.iterator.current_item))

            attributes = existing_feature.attributes

            # update the attributes (using sets for de-duping)
            for k, v in f.attributes.items():
                attributes[k] = list(set(attributes[k]).union(v))
            existing_feature.attributes = attributes
            return existing_feature
        elif self.merge_strategy == 'create_unique':
            f.id = self._increment_featuretype_autoid(f.id)
            return f
        else:
            raise ValueError("Invalid merge strategy '%s'"
                             % (self.merge_strategy))

    def _populate_from_lines(self, lines):
        raise NotImplementedError

    def _update_relations(self):
        raise NotImplementedError

    def _drop_indexes(self):
        c = self.conn.cursor()
        for index in constants.INDEXES:
            c.execute("DROP INDEX IF EXISTS ?", (index,))
        self.conn.commit()

    def _init_tables(self):
        """
        Table creation
        """
        c = self.conn.cursor()
        c.executescript(constants.SCHEMA)
        self.conn.commit()

    def _finalize(self):
        """
        Various last-minute stuff to perform after file has been parsed and
        imported.

        In general, if you'll be adding stuff to the meta table, do it here.
        """
        c = self.conn.cursor()
        c.executemany('''
                      INSERT INTO directives VALUES (?)
                      ''', ((i,) for i in self.iterator.directives))
        c.execute(
            '''
            INSERT INTO meta (version, dialect)
            VALUES (:version, :dialect)''',
            dict(version=version.version,
                 dialect=helpers._jsonify(self.iterator.dialect))
        )

        c.executemany(
            '''
            INSERT OR REPLACE INTO autoincrements VALUES (?, ?)
            ''', self._autoincrements.items())

        self.conn.commit()

        self.warnings = self.iterator.warnings

    def create(self):
        """
        Calls various methods sequentially in order to fully build the
        database.
        """
        # Calls each of these methods in order.  _populate_from_lines and
        # _update_relations must be implemented in subclasses.
        self._init_tables()
        self._populate_from_lines(self.iterator)
        self._update_relations()
        self._finalize()

        # reset logger to whatever it was before...
        logger.setLevel(self._orig_logger_level)

    def update(self, iterator):
        self._populate_from_lines(iterator)
        self._update_relations()

    def execute(self, query):
        """
        Execute a query directly on the database.
        """
        c = self.conn.cursor()
        c.execute(query)
        for i in cursor:
            yield i


class _GFFDBCreator(_DBCreator):
    def __init__(self, *args, **kwargs):
        """
        _DBCreator subclass specifically for working with GFF files.

        create_db() delegates to this class -- see that function for docs
        """
        super(_GFFDBCreator, self).__init__(*args, **kwargs)

    def _populate_from_lines(self, lines):
        c = self.conn.cursor()
        c.execute(
            '''
            PRAGMA synchronous=NORMAL;
            ''')
        c.execute(
            '''
            PRAGMA journal_mode=WAL;
            ''')
        self._drop_indexes()
        last_perc = 0
        logger.info("Populating features")
        msg = ("Populating features table and first-order relations: "
               "%d features\r")

        # ONEBYONE is used for profiling -- how to get faster inserts?
        # ONEBYONE=False will do a single executemany
        # ONEBYONE=True will do many single execute
        #
        # c.executemany() was not as much of an improvement as I had expected.
        #
        # Compared to a benchmark of doing each insert separately:
        # executemany using a list of dicts to iterate over is ~15% slower
        # executemany using a list of tuples to iterate over is ~8% faster
        ONEBYONE = True

        _features, _relations = [], []
        for i, f in enumerate(lines):

            # Percent complete
            if self.verbose:
                if i % 1000 == 0:
                    sys.stderr.write(msg % i)
                    sys.stderr.flush()

            # TODO: handle ID creation here...should be combined with the
            # INSERT below (that is, don't IGNORE below but catch the error and
            # re-try with a new ID).  However, is this doable with an
            # execute-many?
            f.id = self._id_handler(f)

            if ONEBYONE:

                # TODO: these are two, one-by-one execute statements.
                # Profiling shows that this is a slow step. Need to use
                # executemany, which probably means writing to file first.

                try:
                    c.execute(constants._INSERT, f.astuple())
                except sqlite3.IntegrityError:
                    fixed = self._do_merge(f)
                    if self.merge_strategy in ['merge', 'replace']:
                        c.execute(
                            '''
                            UPDATE features SET attributes = ?
                            WHERE id = ?
                            ''', (helpers._jsonify(fixed.attributes),
                                  fixed.id))

                    elif self.merge_strategy == 'create_unique':
                        c.execute(constants._INSERT, f.astuple())

                # Works in all cases since attributes is a defaultdict
                if 'Parent' in f.attributes:
                    for parent in f.attributes['Parent']:
                        c.execute(
                            '''
                            INSERT OR IGNORE INTO relations VALUES
                            (?, ?, 1)
                            ''', (parent, f.id))


            else:
                _features.append(f.astuple())

                if 'Parent' in f.attributes:
                    for parent in f.attributes['Parent']:
                        _relations.append((parent, f.id))

        if not ONEBYONE:
            # Profiling shows that there's an extra overhead for using dict
            # syntax in sqlite3 queries.  Even though we do the lookup above
            # (when appending to _features), it's still faster to use the tuple
            # syntax.
            c.executemany(constants._INSERT, _features)

            c.executemany(
                '''
                INSERT INTO relations VALUES (?,?, 1);
                ''', _relations)

            del _relations
            del _features

        self.conn.commit()
        if self.verbose:
            # i is not set here! Bug?
            i = 0
            sys.stderr.write((msg % i) + '\n')

    def _update_relations(self):
        logger.info("Updating relations")
        c = self.conn.cursor()
        c2 = self.conn.cursor()
        c3 = self.conn.cursor()

        # TODO: pre-compute indexes?
        #c.execute('CREATE INDEX ids ON features (id)')
        #c.execute('CREATE INDEX parentindex ON relations (parent)')
        #c.execute('CREATE INDEX childindex ON relations (child)')
        #self.conn.commit()

        tmp = tempfile.NamedTemporaryFile(delete=False).name
        fout = open(tmp, 'w')

        # Here we look for "grandchildren" -- for each ID, get the child
        # (parenthetical subquery below); then for each of those get *its*
        # child (main query below).
        #
        # Results are written to temp file so that we don't read and write at
        # the same time, which would slow things down considerably.

        c.execute('SELECT id FROM features')
        for parent in c:
            c2.execute('''
                       SELECT child FROM relations WHERE parent IN
                       (SELECT child FROM relations WHERE parent = ?)
                       ''', tuple(parent))
            for grandchild in c2:
                fout.write('\t'.join((parent[0], grandchild[0])) + '\n')
        fout.close()

        def relations_generator():
            for line in open(fout.name):
                parent, child = line.strip().split('\t')
                yield dict(parent=parent, child=child, level=2)

        c.executemany(
            '''
            INSERT OR IGNORE INTO relations VALUES
            (:parent, :child, :level)
            ''', relations_generator())

        # TODO: Index creation.  Which ones affect performance?
        c.execute("DROP INDEX IF EXISTS binindex")
        c.execute("CREATE INDEX binindex ON features (bin)")

        self.conn.commit()
        os.unlink(fout.name)


class _GTFDBCreator(_DBCreator):
    def __init__(self, *args, **kwargs):
        """
        create_db() delegates to this class -- see that function for docs
        """
        self.transcript_key = kwargs.pop('transcript_key', 'transcript_id')
        self.gene_key = kwargs.pop('gene_key', 'gene_id')
        self.subfeature = kwargs.pop('subfeature', 'exon')
        super(_GTFDBCreator, self).__init__(*args, **kwargs)

    def _populate_from_lines(self, lines):
        msg = (
            "Populating features table and first-order relations: %d "
            "features\r"
        )

        c = self.conn.cursor()

        last_perc = 0
        for i, f in enumerate(lines):

            # Percent complete
            if self.verbose:
                if i % 1000 == 0:
                    sys.stderr.write(msg % i)
                    sys.stderr.flush()

            f.id = self._id_handler(f)

            # Insert the feature itself...
            try:
                c.execute(constants._INSERT, f.astuple())
            except sqlite3.IntegrityError:
                fixed = self._do_merge(f)
                if self.merge_strategy in ['merge', 'replace']:
                    c.execute(
                        '''
                        UPDATE features SET attributes = ?
                        WHERE id = ?
                        ''', (helpers._jsonify(fixed.attributes),
                              fixed.id))

                elif self.merge_strategy == 'create_unique':
                    c.execute(constants._INSERT, f.astuple())

            # For an on-spec GTF file,
            # self.transcript_key = "transcript_id"
            # self.gene_key = "gene_id"
            relations = []
            parent = None
            grandparent = None
            if self.transcript_key in f.attributes:
                parent = f.attributes[self.transcript_key][0]
                relations.append((parent, f.id, 1))

            if self.gene_key in f.attributes:
                grandparent = f.attributes[self.gene_key]
                if len(grandparent) > 0:
                    grandparent = grandparent[0]
                    relations.append((grandparent, f.id, 2))
                    if parent is not None:
                        relations.append((grandparent, parent, 1))

            # Note the IGNORE, so relationships defined many times in the file
            # (e.g., the transcript-gene relation on pretty much every line in
            # a GTF) will only be included once.
            c.executemany(
                '''
                INSERT OR IGNORE INTO relations (parent, child, level)
                VALUES (?, ?, ?)
                ''', relations
            )

        self.conn.commit()
        if self.verbose:
            sys.stderr.write((msg % i) + '\n')

    def _update_relations(self):
        # TODO: do any indexes speed this up?

        c = self.conn.cursor()
        c2 = self.conn.cursor()

        tmp = tempfile.NamedTemporaryFile(delete=False).name
        tmp = '/tmp/gffutils'
        fout = open(tmp, 'w')

        self._tmpfile = tmp

        # This takes some explanation...
        #
        # First, the nested subquery gets the level-1 parents of
        # self.subfeature featuretypes.  For an on-spec GTF file,
        # self.subfeature = "exon", so this translates to getting the distinct
        # level-1 parents of exons . . . which are transcripts.
        #
        # OK, so this subquery is now a list of transcripts; call it
        # "firstlevel".
        #
        # Then join firstlevel on relations, but the trick is to now consider
        # each transcript a *child* -- so that relations.parent (on the first
        # line of the query) will be the first-level parent of the transcript
        # (the gene).
        #
        #
        # The result is something like:
        #
        #   transcript1     gene1
        #   transcript2     gene1
        #   transcript3     gene2
        #
        # Note that genes are repeated; below we need to ensure that only one
        # is added.  To ensure this, the results are ordered by the gene ID.

        c.execute(
            '''
            SELECT DISTINCT firstlevel.parent, relations.parent
            FROM (
                SELECT DISTINCT relations.parent
                FROM relations
                JOIN features ON features.id = relations.child
                WHERE features.featuretype = ?
                AND relations.level = 1
            )
            AS firstlevel
            JOIN relations ON firstlevel.parent = child
            WHERE relations.level = 1
            ORDER BY relations.parent
            ''', (self.subfeature,))

        # Now we iterate through those results (using a new cursor) to infer
        # the extent of transcripts and genes.

        last_gene_id = None
        for transcript_id, gene_id in c:
            # transcript extent
            c2.execute(
                '''
                SELECT MIN(start), MAX(end), strand, seqid
                FROM features
                JOIN relations ON
                features.id = relations.child
                WHERE parent = ? AND featuretype == ?
                ''', (transcript_id, self.subfeature))
            transcript_start, transcript_end, strand, seqid = c2.fetchone()
            transcript_attributes = {
                self.transcript_key: [transcript_id],
                self.gene_key: [gene_id]
            }
            transcript_bin = bins.bins(
                transcript_start, transcript_end, one=True)

            # Write out to file; we'll be reading it back in shortly.  Omit
            # score, frame, source, and extra since they will always have the
            # same default values (".", ".", "gffutils_derived", and []
            # respectively)

            fout.write('\t'.join(map(str, [
                transcript_id,
                seqid,
                transcript_start,
                transcript_end,
                strand,
                'transcript',
                transcript_bin,
                helpers._jsonify(transcript_attributes)
            ])) + '\n')

            # Infer gene extent, but only if we haven't done so already.
            if gene_id != last_gene_id:
                c2.execute(
                    '''
                    SELECT MIN(start), MAX(end), strand, seqid
                    FROM features
                    JOIN relations ON
                    features.id = relations.child
                    WHERE parent = ? AND featuretype == ?
                    ''', (gene_id, self.subfeature))
                gene_start, gene_end, strand, seqid = c2.fetchone()
                gene_attributes = {self.gene_key: [gene_id]}
                gene_bin = bins.bins(gene_start, gene_end, one=True)

                fout.write('\t'.join(map(str, [
                    gene_id,
                    seqid,
                    gene_start,
                    gene_end,
                    strand,
                    'gene',
                    gene_bin,
                    helpers._jsonify(gene_attributes)
                ])) + '\n')

            last_gene_id = gene_id

        fout.close()

        def derived_feature_generator():
            """
            Generator of items from the file that was just created...
            """
            keys = ['parent', 'seqid', 'start', 'end', 'strand',
                    'featuretype', 'bin', 'attributes']
            for line in open(fout.name):
                d = dict(zip(keys, line.strip().split('\t')))
                d.pop('parent')
                d['score'] = '.'
                d['source'] = 'gffutils_derived'
                d['frame'] = '.'
                d['extra'] = []
                d['attributes'] = helpers._unjsonify(d['attributes'])
                f = feature.Feature(**d)
                f.id = self._id_handler(f)
                yield f

        # Insert the just-inferred transcripts and genes.  We should always do
        # assume merge_strategy="merge", since these derived features take into
        # account the current state of the db.
        for f in derived_feature_generator():
            try:
                c.execute(constants._INSERT, f.astuple())
            except sqlite3.IntegrityError:
                fixed = self._do_merge(f)
                c.execute(
                    '''
                    UPDATE features SET attributes = ?
                    WHERE id = ?
                    ''', (helpers._jsonify(fixed.attributes),
                          fixed.id))

        self.conn.commit()
        os.unlink(fout.name)

        # TODO: recreate indexes?


def create_db(data, dbfn, id_spec=None, force=False, verbose=True,
              checklines=10, merge_strategy='error', transform=None,
              gtf_transcript_key='transcript_id', gtf_gene_key='gene_id',
              gtf_subfeature='exon', force_gff=False,
              force_dialect_check=False, from_string=False):
    """
    Create a database from a GFF or GTF file.

    Parameters
    ----------
    `data` : string or iterable

        If a string (and `from_string` is False), then `data` is the path to
        the original GFF or GTF file.

        If a string and `from_string` is True, then assume `data` is the actual
        data to use.

        Otherwise, it's an iterable of Feature objects.

    `dbfn` : string

        Path to the database that will be created.  Can be the special string
        ":memory:" to create an in-memory database.

    `id_spec` : string, list, dict, callable, or None

        This parameter guides what will be used as the primary key for the
        database, which in turn determines how you will access individual
        features by name from the database.

        If `id_spec=None`, then auto-increment primary keys based on the
        feature type (e.g., "gene_1", "gene_2").  This is also the fallback
        behavior for the other values below.

        If `id_spec` is a string, then look for this key in the attributes.  If
        it exists, then use its value as the primary key, otherwise
        autoincrement based on the feature type.  For many GFF3 files, "ID"
        usually works well.

        If `id_spec` is a list or tuple of keys, then check for each one in
        order, using the first one found.  For GFF3, this might be ["ID",
        "Name"], which would use the ID if it exists, otherwise the Name,
        otherwise autoincrement based on the feature type.

        If `id_spec` is a dictionary, then it is a mapping of feature types to
        what should be used as the ID.  For example, for GTF files, `{'gene':
        'gene_id', 'transcript': 'transcript_id'}` may be useful.  The values
        of this dictionary can also be a list, e.g., `{'gene': ['gene_id',
        'geneID']}`

        If `id_spec` is a callable object, then it accepts a dictionary from
        the iterator and returns one of the following:

            * None (in which case the feature type will be auto-incremented)
            * string (which will be used as the primary key)
            * special string starting with "autoincrement:X", where "X" is
              a string that will be used for auto-incrementing.  For example,
              if "autoincrement:chr10", then the first feature will be
              "chr10_1", the second "chr10_2", and so on.

    `force` : bool

        If `False` (default), then raise an exception if `dbfn` already exists.
        Use `force=True` to overwrite any existing databases.

    `verbose` : bool

        Report percent complete and other feedback on how the db creation is
        progressing.

        In order to report percent complete, the entire file needs to be read
        once to see how many items there are; for large files you may want to
        use `verbose=False` to avoid this.

    `checklines` : int

        Number of lines to check the dialect.

    `merge_strategy` : { "merge", "create_unique", "error", "warning" }

        This parameter specifies the behavior when two items have an identical
        primary key.

        Using `merge_strategy="merge"`, then there will be a single entry in
        the database, but the attributes of all features with the same primary
        key will be merged.

        Using `merge_strategy="create_unique"`, then the first entry will use
        the original primary key, but the second entry will have a unique,
        autoincremented primary key assigned to it

        Using `merge_strategy="error"`, a :class:`gffutils.DuplicateID`
        exception will be raised.  This means you will have to edit the file
        yourself to fix the duplicated IDs.

        Using `merge_strategy="warning"`, a warning will be printed to the
        logger, and the duplicate feature will be skipped.

    `transform` : callable

        Function (or other callable object) that accepts a dictionary and
        returns a dictionary.

    `gtf_transcript_key`, `gtf_gene_key` : string

        Which attribute to use as the transcript ID and gene ID respectively
        for GTF files.  Default is `transcript_id` and `gene_id` according to
        the GTF spec.

    `gtf_subfeature` : string

        Feature type to use as a "gene component" when inferring gene and
        transcript extents for GTF files.  Default is `exon` according to the
        GTF spec.

    `force_gff` : bool
        If True, do not do automatic format detection -- only use GFF.

    `force_dialect_check`: bool
        If True, the dialect will be checkef for every feature (instead of just
        `checklines` features).  This can be slow, but may be necessary for
        inconsistently-formatted input files.

    `from_string`: bool
        If True, then treat `data` as actual data (rather than the path to
        a file).
    """
    kwargs = dict(
        data=data, checklines=checklines, transform=transform,
        force_dialect_check=force_dialect_check, from_string=from_string)

    # First construct an iterator so that we can identify the file format.
    # DataIterator figures out what kind of data was provided (string of lines,
    # filename, or iterable of Features) and checks `checklines` lines to
    # identify the dialect.
    iterator = iterators.DataIterator(**kwargs)
    dialect = iterator.dialect

    # However, a side-effect of this is that  if `data` was a generator, then
    # we've just consumed `checklines` items (see
    # iterators.BaseIterator.__init__, which calls iterators.peek).
    #
    # But it also chains those consumed items back onto the beginning, and the
    # result is available as as iterator._iter.
    #
    # That's what we should be using now for `data:
    kwargs['data'] = iterator._iter

    # Since we've already checked lines, we don't want to do it again
    kwargs['checklines'] = 0

    if force_gff or (dialect['fmt'] == 'gff3'):
        cls = _GFFDBCreator
        id_spec = id_spec or 'ID'
        add_kwargs = {}
    elif dialect['fmt'] == 'gtf':
        cls = _GTFDBCreator
        id_spec = id_spec or {'gene': 'gene_id', 'transcript': 'transcript_id'}
        add_kwargs = dict(
            transcript_key=gtf_transcript_key,
            gene_key=gtf_gene_key,
            subfeature=gtf_subfeature)

    kwargs.update(**add_kwargs)
    kwargs['dialect'] = dialect
    c = cls(dbfn=dbfn, id_spec=id_spec, force=force, verbose=verbose,
            merge_strategy=merge_strategy, **kwargs)

    c.create()
    db = interface.FeatureDB(c)

    return db
