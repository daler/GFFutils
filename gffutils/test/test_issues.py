"""
Tests for specific issues and pull requests
"""


import os
import tempfile
import difflib
from textwrap import dedent
import gffutils
from gffutils import feature


def test_issue_79():
    gtf = gffutils.example_filename("keep-order-test.gtf")
    db = gffutils.create_db(
        gtf,
        "tmp.db",
        disable_infer_genes=False,
        disable_infer_transcripts=False,
        id_spec={"gene": "gene_id", "transcript": "transcript_id"},
        merge_strategy="create_unique",
        keep_order=True,
        force=True,
    )

    exp = open(gtf).read()
    obs = "\n".join([str(i) for i in db.all_features()])
    exp_1 = exp.splitlines(True)[0].strip()
    obs_1 = obs.splitlines(True)[0].strip()
    print("EXP")
    print(exp_1)
    print("OBS")
    print(obs_1)
    print("DIFF")
    print("".join(difflib.ndiff([exp_1], [obs_1])))
    assert obs_1 == exp_1


def test_issue_82():
    # key-val separator is inside an unquoted attribute value
    x = (
        "Spenn-ch12\tsgn_markers\tmatch\t2621812\t2622049\t.\t+\t.\t"
        "Alias=SGN-M1347;ID=T0028;Note=marker name(s): T0028 SGN-M1347 |identity=99.58|escore=2e-126"
    )
    y = feature.feature_from_line(x)
    assert y.attributes["Note"] == [
        "marker name(s): T0028 SGN-M1347 |identity=99.58|escore=2e-126"
    ]

    gffutils.create_db(gffutils.example_filename("keyval_sep_in_attrs.gff"), ":memory:")


def test_issue_85():
    # when start or stop was empty, #85 would fail Should now work with
    # blank fields
    f = feature.feature_from_line("\t".join([""] * 9))

    # or with "." placeholders
    f = feature.feature_from_line("\t".join(["."] * 9))


def test_issue_105():
    fn = gffutils.example_filename("FBgn0031208.gtf")
    home = os.path.expanduser("~")
    newfn = os.path.join(home, ".gffutils.test")
    with open(newfn, "w") as fout:
        fout.write(open(fn).read())
    f = gffutils.iterators.DataIterator(newfn)
    for i in f:
        pass
    os.unlink(newfn)


def test_issue_107():
    s = dedent(
        """
        chr1\t.\tgene\t10\t15\t.\t+\t.\tID=b;
        chr1\t.\tgene\t1\t5\t.\t-\t.\tID=a;
        chr2\t.\tgene\t25\t50\t.\t-\t.\tID=c;
        chr2\t.\tgene\t55\t60\t.\t-\t.\tID=d;
        """
    )
    tmp = tempfile.NamedTemporaryFile(delete=False).name
    with open(tmp, "w") as fout:
        fout.write(s + "\n")
    db = gffutils.create_db(tmp, ":memory:")
    interfeatures = list(
        db.interfeatures(db.features_of_type("gene", order_by=("seqid", "start")))
    )
    assert [str(i) for i in interfeatures] == [
        "chr1\tgffutils_derived\tinter_gene_gene\t6\t9\t.\t.\t.\tID=a,b;",
        "chr2\tgffutils_derived\tinter_gene_gene\t16\t54\t.\t-\t.\tID=c,d;",
    ]


def test_issue_119():
    # First file has these two exons with no ID:
    #
    #   chr2L FlyBase exon  8193  8589  .  +  .  Parent=FBtr0300690
    #   chr2L FlyBase exon  7529  8116  .  +  .  Name=CG11023:1;Parent=FBtr0300689,FBtr0300690
    #
    db0 = gffutils.create_db(gffutils.example_filename("FBgn0031208.gff"), ":memory:")

    # And this one, a bunch of reads with no IDs anywhere
    db1 = gffutils.create_db(
        gffutils.example_filename("F3-unique-3.v2.gff"), ":memory:"
    )

    # When db1 is updated by db0
    db2 = db1.update(db0)
    assert (
        db2._autoincrements == db1._autoincrements == {"exon": 2, "read": 112}
    ), db2._autoincrements

    assert len(list(db0.features_of_type("exon"))) == 6

    # Now we update that with db0 again
    db3 = db2.update(db0, merge_strategy="replace")

    # Using the "replace" strategy, we should have only gotten another 2 exons
    assert len(list(db3.features_of_type("exon"))) == 8

    # Make sure that the autoincrements for exons jumped by 2
    assert (
        db2._autoincrements == db3._autoincrements == {"exon": 4, "read": 112}
    ), db2._autoincrements

    # More isolated test, merging two databases each created from the same file
    # which itself contains only a single feature with no ID.
    tmp = tempfile.NamedTemporaryFile(delete=False).name
    with open(tmp, "w") as fout:
        fout.write("chr1\t.\tgene\t10\t15\t.\t+\t.\t\n")

    db4 = gffutils.create_db(tmp, tmp + ".db")
    db5 = gffutils.create_db(tmp, ":memory:")

    assert db4._autoincrements == {"gene": 1}
    assert db5._autoincrements == {"gene": 1}

    db6 = db4.update(db5)

    db7 = gffutils.FeatureDB(db4.dbfn)

    # both db4 and db6 should now have the same, updated autoincrements because
    # they both point to the same db.
    assert db6._autoincrements == db4._autoincrements == {"gene": 2}

    # But db5 was created independently and should have unchanged autoincrements
    assert db5._autoincrements == {"gene": 1}

    # db7 was created from the database pointed to by both db4 and db6. This
    # tests that when a FeatureDB is created it should have the
    # correctly-updated autoincrements read from the db
    assert db7._autoincrements == {"gene": 2}


def test_pr_131():
    db = gffutils.create_db(gffutils.example_filename("FBgn0031208.gff"), ":memory:")

    # previously would raise ValueError("No lines parsed -- was an empty
    # file provided?"); now just does nothing
    db2 = db.update([])


def test_pr_133():
    # Previously, merge_attributes would not deep-copy the values from the
    # second dict, and when the values are then modified, the second dict is
    # unintentionally modified.
    d1 = {"a": [1]}
    d2 = {"a": [2]}
    d1a = {"a": [1]}
    d2a = {"a": [2]}
    d3 = gffutils.helpers.merge_attributes(d1, d2)
    assert d1 == d1a, d1
    assert d2 == d2a, d2


def test_pr_139():
    db = gffutils.create_db(gffutils.example_filename("FBgn0031208.gff"), ":memory:")
    exons = list(db.features_of_type("exon"))
    inter = list(db.interfeatures(exons))

    # previously, the first exon's attributes would show up in subsequent merged features
    assert exons[0].attributes["Name"][0] not in inter[1].attributes["Name"]
    assert exons[0].attributes["Name"][0] not in inter[2].attributes["Name"]
    assert exons[0].attributes["Name"][0] not in inter[3].attributes["Name"]


def test_pr_144():
    # previously this would fail with:
    #   UnboundLocalError: local variable 'part' referenced before assignment
    f = gffutils.Feature(attributes={"a": [""]})

    # Make sure everything got converted correctly
    assert f.attributes["a"] == [""]
    assert str(f) == ".	.	.	.	.	.	.	.	a"
    g = gffutils.feature.feature_from_line(str(f))
    assert g == f


def test_pr_172():
    line = (
        "NC_049222.1\tGnomon\tgene\t209085\t282880\t.\t-\t.\t"
        'gene_id "ENPP1_3"; transcript_id ""; db_xref "GeneID:100856150";'
        'db_xref "VGNC:VGNC:40374"; gbkey "Gene"; gene "ENPP1"; '
        'gene_biotype "protein_coding";\n'
    )
    tmp = tempfile.NamedTemporaryFile(delete=False).name
    with open(tmp, "w") as fout:
        fout.write(line)
    db = gffutils.create_db(tmp, ":memory:")


def test_pr_171():
    q = gffutils.parser.Quoter()
    assert q.__missing__("\n") == "%0A"
    assert q.__missing__("a") == "a"

    assert q.__missing__("") == ""


def test_issue_129():

    # thanks @Brunox13 for the detailed notes on #129

    line = 'chr1\tdemo\tstart_codon\t69091\t69093\t.\t+\t.\tgene_id "demo";\n'
    tmp = tempfile.NamedTemporaryFile(delete=False).name
    with open(tmp, "w") as fout:
        fout.write(line)
    db = gffutils.create_db(tmp, ":memory:")

    # ASCII art to visualize each test (coords are along the top, from 69087 to
    # 69090). The tests slide a 4-bp region over the original 3-bp start codon.

    # 7 8 9 0 1 2 3 4 5 6 7
    #         | | |         Orig feature
    # | | | |               Test feature
    res = list(db.region(region=("chr1", 69087, 69090), featuretype="start_codon"))
    assert len(res) == 0

    # NOTE: prior to #162, this did not return anything
    # 7 8 9 0 1 2 3 4 5 6 7
    #         | | |         Orig feature
    #   | | | |             Test feature
    res = list(db.region(region=("chr1", 69088, 69091), featuretype="start_codon"))
    assert len(res) == 1

    # 7 8 9 0 1 2 3 4 5 6 7
    #         | | |         Orig feature
    #     | | | |           Test feature
    res = list(db.region(region=("chr1", 69089, 69092), featuretype="start_codon"))
    assert len(res) == 1

    # 7 8 9 0 1 2 3 4 5 6 7
    #         | | |         Orig feature
    #       | | | |         Test feature
    res = list(db.region(region=("chr1", 69090, 69093), featuretype="start_codon"))
    assert len(res) == 1

    # 7 8 9 0 1 2 3 4 5 6 7
    #         | | |         Orig feature
    #         | | | |       Test feature
    res = list(db.region(region=("chr1", 69091, 69094), featuretype="start_codon"))
    assert len(res) == 1

    # 7 8 9 0 1 2 3 4 5 6 7
    #         | | |         Orig feature
    #           | | | |     Test feature
    res = list(db.region(region=("chr1", 69092, 69095), featuretype="start_codon"))
    assert len(res) == 1

    # NOTE: priro to #162, this did not return anything
    # 7 8 9 0 1 2 3 4 5 6 7
    #         | | |         Orig feature
    #             | | | |   Test feature
    res = list(db.region(region=("chr1", 69093, 69096), featuretype="start_codon"))
    assert len(res) == 1

    # 7 8 9 0 1 2 3 4 5 6 7
    #         | | |         Orig feature
    #               | | | | Test feature
    res = list(db.region(region=("chr1", 69094, 69097), featuretype="start_codon"))
    assert len(res) == 0
