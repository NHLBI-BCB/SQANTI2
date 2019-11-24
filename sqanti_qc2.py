#!/usr/bin/env python
# SQANTI: Structural and Quality Annotation of Novel Transcript Isoforms
# Authors: Lorena de la Fuente, Hector del Risco, Cecile Pereira and Manuel Tardaguila
# Modified by Liz (etseng@pacb.com) currently as SQANTI2 working version

__author__  = "etseng@pacb.com"
__version__ = '5.1.0'  # Python 3.7

import pdb
import os, re, sys, subprocess, timeit, glob
import distutils.spawn
import itertools
import bisect
import argparse
import math
from collections import defaultdict, Counter
from csv import DictWriter, DictReader

utilitiesPath =  os.path.dirname(os.path.realpath(__file__))+"/utilities/" 
sys.path.insert(0, utilitiesPath)
from rt_switching import rts
from indels_annot import calc_indels_from_sam


try:
    from Bio.Seq import Seq
    from Bio import SeqIO
    from Bio.SeqRecord import SeqRecord
except ImportError:
    print("Unable to import Biopython! Please make sure Biopython is installed.", file=sys.stderr)
    sys.exit(-1)

try:
    from bx.intervals import Interval, IntervalTree
except ImportError:
    print("Unable to import bx-python! Please make sure bx-python is installed.", file=sys.stderr)
    sys.exit(-1)

try:
    from BCBio import GFF as BCBio_GFF
except ImportError:
    print("Unable to import BCBio! Please make sure bcbiogff is installed.", file=sys.stderr)
    sys.exit(-1)

try:
    from err_correct_w_genome import err_correct
    from sam_to_gff3 import convert_sam_to_gff3
    from STAR import STARJunctionReader
    from BED import LazyBEDPointReader
    import coordinate_mapper as cordmap
except ImportError:
    print("Unable to import err_correct_w_genome or sam_to_gff3.py! Please make sure cDNA_Cupcake/sequence/ is in $PYTHONPATH.", file=sys.stderr)
    sys.exit(-1)

try:
    from cupcake.tofu.compare_junctions import compare_junctions
    from cupcake.tofu.filter_away_subset import read_count_file
    from cupcake.io.BioReaders import GMAPSAMReader
    from cupcake.io.GFF import collapseGFFReader, write_collapseGFF_format
except ImportError:
    print("Unable to import cupcake.tofu! Please make sure you install cupcake.", file=sys.stderr)
    sys.exit(-1)

# check cupcake version
import cupcake
v1, v2 = [int(x) for x in cupcake.__version__.split('.')]
if v1 < 8 or v2 < 6:
    print("Cupcake version must be 8.6 or higher! Got {0} instead.".format(cupcake.__version__), file=sys.stderr)
    sys.exit(-1)


GMAP_CMD = "gmap --cross-species -n 1 --max-intronlength-middle=2000000 --max-intronlength-ends=2000000 -L 3000000 -f samse -t {cpus} -D {dir} -d {name} -z {sense} {i} > {o}"
#MINIMAP2_CMD = "minimap2 -ax splice --secondary=no -C5 -O6,24 -B4 -u{sense} -t {cpus} {g} {i} > {o}"
MINIMAP2_CMD = "minimap2 -ax splice --secondary=no -C5 -u{sense} -t {cpus} {g} {i} > {o}"
DESALT_CMD = "deSALT aln {dir} {i} -t {cpus} -x ccs -o {o}"

GMSP_PROG = os.path.join(utilitiesPath, "gmst", "gmst.pl")
GMST_CMD = "perl " + GMSP_PROG + " -faa --strand direct --fnn --output {o} {i}"

GTF2GENEPRED_PROG = "gtfToGenePred"
GFFREAD_PROG = "gffread"

if distutils.spawn.find_executable(GTF2GENEPRED_PROG) is None:
    print("Cannot find executable {0}. Abort!".format(GTF2GENEPRED_PROG), file=sys.stderr)
    sys.exit(-1)
if distutils.spawn.find_executable(GFFREAD_PROG) is None:
    print("Cannot find executable {0}. Abort!".format(GFFREAD_PROG), file=sys.stderr)
    sys.exit(-1)


seqid_rex1 = re.compile('PB\.(\d+)\.(\d+)$')
seqid_rex2 = re.compile('PB\.(\d+)\.(\d+)\|\S+')
seqid_fusion = re.compile("PBfusion\.(\d+)")


FIELDS_JUNC = ['isoform', 'chrom', 'strand', 'junction_number', 'genomic_start_coord',
                   'genomic_end_coord', 'transcript_coord', 'junction_category',
                   'start_site_category', 'end_site_category', 'diff_to_Ref_start_site',
                   'diff_to_Ref_end_site', 'bite_junction', 'splice_site', 'canonical',
                   'RTS_junction', 'indel_near_junct',
                   'phyloP_start', 'phyloP_end', 'sample_with_cov', "total_coverage"] #+coverage_header

FIELDS_CLASS = ['isoform', 'chrom', 'strand', 'length',  'exons',  'structural_category',
                'associated_gene', 'associated_transcript',  'ref_length', 'ref_exons',
                'diff_to_TSS', 'diff_to_TTS', 'diff_to_gene_TSS', 'diff_to_gene_TTS',
                'subcategory', 'RTS_stage', 'all_canonical',
                'min_sample_cov', 'min_cov', 'min_cov_pos',  'sd_cov', 'FL', 'n_indels',
                'n_indels_junc',  'bite',  'iso_exp', 'gene_exp',  'ratio_exp',
                'FSM_class',   'coding', 'ORF_length', 'CDS_length', 'CDS_start',
                'CDS_end', 'CDS_genomic_start', 'CDS_genomic_end', 'predicted_NMD',
                'perc_A_downstream_TTS', 'dist_to_cage_peak', 'within_cage_peak',
                'polyA_motif', 'polyA_dist']

RSCRIPTPATH = distutils.spawn.find_executable('Rscript')
RSCRIPT_REPORT = 'SQANTI_report2.R'

if os.system(RSCRIPTPATH + " --version")!=0:
    print("Rscript executable not found! Abort!", file=sys.stderr)
    sys.exit(-1)


class genePredReader(object):
    def __init__(self, filename):
        self.f = open(filename)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.f.readline().strip()
        if len(line) == 0:
            raise StopIteration
        return genePredRecord.from_line(line)


class genePredRecord(object):
    def __init__(self, id, chrom, strand, txStart, txEnd, cdsStart, cdsEnd, exonCount, exonStarts, exonEnds, gene=None):
        self.id = id
        self.chrom = chrom
        self.strand = strand
        self.txStart = txStart         # 1-based start
        self.txEnd = txEnd             # 1-based end
        self.cdsStart = cdsStart       # 1-based start
        self.cdsEnd = cdsEnd           # 1-based end
        self.exonCount = exonCount
        self.exonStarts = exonStarts   # 0-based starts
        self.exonEnds = exonEnds       # 1-based ends
        self.gene = gene

        self.length = 0
        self.exons = []

        for s,e in zip(exonStarts, exonEnds):
            self.length += e-s
            self.exons.append(Interval(s, e))

        # junctions are stored (1-based last base of prev exon, 1-based first base of next exon)
        self.junctions = [(self.exonEnds[i],self.exonStarts[i+1]) for i in range(self.exonCount-1)]

    @property
    def segments(self):
        return self.exons


    @classmethod
    def from_line(cls, line):
        raw = line.strip().split('\t')
        return cls(id=raw[0],
                  chrom=raw[1],
                  strand=raw[2],
                  txStart=int(raw[3]),
                  txEnd=int(raw[4]),
                  cdsStart=int(raw[5]),
                  cdsEnd=int(raw[6]),
                  exonCount=int(raw[7]),
                  exonStarts=[int(x) for x in raw[8][:-1].split(',')],  #exonStarts string has extra , at end
                  exonEnds=[int(x) for x in raw[9][:-1].split(',')],     #exonEnds string has extra , at end
                  gene=raw[11] if len(raw)>=12 else None,
                  )

    def get_splice_site(self, genome_dict, i):
        """
        Return the donor-acceptor site (ex: GTAG) for the i-th junction
        :param i: 0-based junction index
        :param genome_dict: dict of chrom --> SeqRecord
        :return: splice site pattern, ex: "GTAG", "GCAG" etc
        """
        assert 0 <= i < self.exonCount-1

        d = self.exonEnds[i]
        a = self.exonStarts[i+1]

        seq_d = genome_dict[self.chrom].seq[d:d+2]
        seq_a = genome_dict[self.chrom].seq[a-2:a]

        if self.strand == '+':
            return (str(seq_d)+str(seq_a)).upper()
        else:
            return (str(seq_a.reverse_complement())+str(seq_d.reverse_complement())).upper()



class myQueryTranscripts:
    def __init__(self, id, tss_diff, tts_diff, num_exons, length, str_class, subtype=None,
                 genes=None, transcripts=None, chrom=None, strand=None, bite ="NA",
                 RT_switching ="????", canonical="NA", min_cov ="NA",
                 min_cov_pos ="NA", min_samp_cov="NA", sd ="NA", FL ="NA", FL_dict={},
                 nIndels ="NA", nIndelsJunc ="NA", proteinID=None,
                 ORFlen="NA", CDS_start="NA", CDS_end="NA",
                 CDS_genomic_start="NA", CDS_genomic_end="NA", is_NMD="NA",
                 isoExp ="NA", geneExp ="NA", coding ="non_coding",
                 refLen ="NA", refExons ="NA",
                 FSM_class = None, percAdownTTS = None,
                 dist_cage='NA', within_cage='NA',
                 polyA_motif='NA', polyA_dist='NA'):

        self.id  = id
        self.tss_diff    = tss_diff   # distance to TSS of best matching ref
        self.tts_diff    = tts_diff   # distance to TTS of best matching ref
        self.tss_gene_diff = 'NA'     # min distance to TSS of all genes matching the ref
        self.tts_gene_diff = 'NA'     # min distance to TTS of all genes matching the ref
        self.genes 		 = genes if genes is not None else []
        self.AS_genes    = set()   # ref genes that are hit on the opposite strand
        self.transcripts = transcripts if transcripts is not None else []
        self.num_exons = num_exons
        self.length      = length
        self.str_class   = str_class  	# structural classification of the isoform
        self.chrom       = chrom
        self.strand 	 = strand
        self.subtype 	 = subtype
        self.RT_switching= RT_switching
        self.canonical   = canonical
        self.min_samp_cov = min_samp_cov
        self.min_cov     = min_cov
        self.min_cov_pos = min_cov_pos
        self.sd 	     = sd
        self.proteinID   = proteinID
        self.ORFlen      = ORFlen
        self.CDS_start   = CDS_start
        self.CDS_end     = CDS_end
        self.coding      = coding
        self.CDS_genomic_start = CDS_genomic_start  # 1-based genomic coordinate of CDS start - strand aware
        self.CDS_genomic_end = CDS_genomic_end      # 1-based genomic coordinate of CDS end - strand aware
        self.is_NMD      = is_NMD                   # (TRUE,FALSE) for NMD if is coding, otherwise "NA"
        self.FL          = FL                       # count for a single sample
        self.FL_dict     = FL_dict                  # dict of sample -> FL count
        self.nIndels     = nIndels
        self.nIndelsJunc = nIndelsJunc
        self.isoExp      = isoExp
        self.geneExp     = geneExp
        self.refLen      = refLen
        self.refExons    = refExons
        self.FSM_class   = FSM_class
        self.bite        = bite
        self.percAdownTTS = percAdownTTS
        self.dist_cage   = dist_cage
        self.within_cage = within_cage
        self.polyA_motif = polyA_motif
        self.polyA_dist  = polyA_dist

    def get_total_diff(self):
        return abs(self.tss_diff)+abs(self.tts_diff)

    def modify(self, ref_transcript, ref_gene, tss_diff, tts_diff, refLen, refExons):
        self.transcripts = [ref_transcript]
        self.genes = [ref_gene]
        self.tss_diff = tss_diff
        self.tts_diff = tts_diff
        self.refLen = refLen
        self.refExons = refExons

    def geneName(self):
        geneName = "_".join(set(self.genes))
        return geneName

    def ratioExp(self):
        if self.geneExp == 0 or self.geneExp == "NA":
            return "NA"
        else:
            ratio = float(self.isoExp)/float(self.geneExp)
        return(ratio)

    def CDSlen(self):
        if self.coding == "coding":
            return(str(int(self.CDS_end) - int(self.CDS_start) + 1))
        else:
            return("NA")

    def __str__(self):
        return "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s" % (self.chrom, self.strand,
                                                                                                                                                           str(self.length), str(self.num_exons),
                                                                                                                                                           str(self.str_class), "_".join(set(self.genes)),
                                                                                                                                                           self.id, str(self.refLen), str(self.refExons),
                                                                                                                                                           str(self.tss_diff), str(self.tts_diff),
                                                                                                                                                           self.subtype, self.RT_switching,
                                                                                                                                                           self.canonical, str(self.min_samp_cov),
                                                                                                                                                           str(self.min_cov), str(self.min_cov_pos),
                                                                                                                                                           str(self.sd), str(self.FL), str(self.nIndels),
                                                                                                                                                           str(self.nIndelsJunc), self.bite, str(self.isoExp),
                                                                                                                                                           str(self.geneExp), str(self.ratioExp()),
                                                                                                                                                           self.FSM_class, self.coding, str(self.ORFlen),
                                                                                                                                                           str(self.CDSlen()), str(self.CDS_start), str(self.CDS_end),
                                                                                                                                                           str(self.CDS_genomic_start), str(self.CDS_genomic_end), str(self.is_NMD),
                                                                                                                                                           str(self.percAdownTTS),
                                                                                                                                                           str(self.dist_cage),
                                                                                                                                                           str(self.within_cage),
                                                                                                                                                           str(self.polyA_motif),
                                                                                                                                                           str(self.polyA_dist))


    def as_dict(self):
        d = {'isoform': self.id,
         'chrom': self.chrom,
         'strand': self.strand,
         'length': self.length,
         'exons': self.num_exons,
         'structural_category': self.str_class,
         'associated_gene': "_".join(set(self.genes)),
         'associated_transcript': "_".join(set(self.transcripts)),
         'ref_length': self.refLen,
         'ref_exons': self.refExons,
         'diff_to_TSS': self.tss_diff,
         'diff_to_TTS': self.tts_diff,
         'diff_to_gene_TSS': self.tss_gene_diff,
         'diff_to_gene_TTS': self.tts_gene_diff,
         'subcategory': self.subtype,
         'RTS_stage': self.RT_switching,
         'all_canonical': self.canonical,
         'min_sample_cov': self.min_samp_cov,
         'min_cov': self.min_cov,
         'min_cov_pos': self.min_cov_pos,
         'sd_cov': self.sd,
         'FL': self.FL,
         'n_indels': self.nIndels,
         'n_indels_junc': self.nIndelsJunc,
         'bite': self.bite,
         'iso_exp': self.isoExp,
         'gene_exp': self.geneExp,
         'ratio_exp': self.ratioExp(),
         'FSM_class': self.FSM_class,
         'coding': self.coding,
         'ORF_length': self.ORFlen,
         'CDS_length': self.CDSlen(),
         'CDS_start': self.CDS_start,
         'CDS_end': self.CDS_end,
         'CDS_genomic_start': self.CDS_genomic_start,
         'CDS_genomic_end': self.CDS_genomic_end,
         'predicted_NMD': self.is_NMD,
         'perc_A_downstream_TTS': self.percAdownTTS,
         'dist_to_cage_peak': self.dist_cage,
         'within_cage_peak': self.within_cage,
         'polyA_motif': self.polyA_motif,
         'polyA_dist': self.polyA_dist
         }
        for sample,count in self.FL_dict.items():
            d["FL."+sample] = count
        return d

class myQueryProteins:

    def __init__(self, cds_start, cds_end, orf_length, proteinID="NA"):
        self.orf_length  = orf_length
        self.cds_start   = cds_start       # 1-based start on transcript
        self.cds_end     = cds_end         # 1-based end on transcript (stop codon), ORF is seq[cds_start-1:cds_end].translate()
        self.cds_genomic_start = None      # 1-based genomic start of ORF, if - strand, is greater than end
        self.cds_genomic_end = None        # 1-based genomic end of ORF
        self.proteinID   = proteinID


def rewrite_sam_for_fusion_ids(sam_filename):
    seen_id_counter = Counter()

    f = open(sam_filename+'.tmp', 'w')
    for line in open(sam_filename):
        if line.startswith('@'):
            f.write(line)
        else:
            raw = line.strip().split('\t')
            if not raw[0].startswith('PBfusion.'):
                print("Expecting fusion ID format `PBfusion.X` but saw {0} instead. Abort!".format(raw[0]), file=sys.stderr)
                sys.exit(-1)
            seen_id_counter[raw[0]] += 1
            raw[0] = raw[0] + '.' + str(seen_id_counter[raw[0]])
            f.write("\t".join(raw) + '\n')
    f.close()
    os.rename(f.name, sam_filename)
    return sam_filename


def write_collapsed_GFF_with_CDS(isoforms_info, input_gff, output_gff):
    """
    Augment a collapsed GFF with CDS information
    *NEW* Also, change the "gene_id" field to use the classification result
    :param isoforms_info: dict of id -> QueryTranscript
    :param input_gff:  input GFF filename
    :param output_gff: output GFF filename
    """
    with open(output_gff, 'w') as f:
        reader = collapseGFFReader(input_gff)
        for r in reader:
            r.geneid = isoforms_info[r.seqid].geneName()  # set the gene name

            s = isoforms_info[r.seqid].CDS_genomic_start  # could be 'NA'
            e = isoforms_info[r.seqid].CDS_genomic_end    # could be 'NA'
            r.cds_exons = []
            if s!='NA' and e!='NA': # has ORF prediction for this isoform
                if r.strand == '+':
                    assert s < e
                    s = s - 1 # make it 0-based
                else:
                    assert e < s
                    s, e = e, s
                    s = s - 1 # make it 0-based
                for i,exon in enumerate(r.ref_exons):
                    if exon.end > s: break
                r.cds_exons = [Interval(s, min(e,exon.end))]
                for exon in r.ref_exons[i+1:]:
                    if exon.start > e: break
                    r.cds_exons.append(Interval(exon.start, min(e, exon.end)))
            write_collapseGFF_format(f, r)


def correctionPlusORFpred(args, genome_dict):
    """
    Use the reference genome to correct the sequences (unless a pre-corrected GTF is given)
    """
    global corrORF
    global corrGTF
    global corrSAM
    global corrFASTA

    corrPathPrefix = os.path.join(args.dir, os.path.splitext(os.path.basename(args.isoforms))[0])
    corrGTF = corrPathPrefix +"_corrected.gtf"
    corrSAM = corrPathPrefix +"_corrected.sam"
    corrFASTA = corrPathPrefix +"_corrected.fasta"
    corrORF =  corrPathPrefix +"_corrected.faa"


    # Step 1. IF GFF or GTF is provided, make it into a genome-based fasta
    #         IF sequence is provided, align as SAM then correct with genome
    if os.path.exists(corrFASTA):
        print("Error corrected FASTA {0} already exists. Using it...".format(corrFASTA), file=sys.stderr)
    else:
        if not args.gtf:
            if os.path.exists(corrSAM):
                print("Aligned SAM {0} already exists. Using it...".format(corrSAM), file=sys.stderr)
            else:
                if args.aligner_choice == "gmap":
                    print("****Aligning reads with GMAP...", file=sys.stdout)
                    cmd = GMAP_CMD.format(cpus=args.gmap_threads,
                                          dir=os.path.dirname(args.gmap_index),
                                          name=os.path.basename(args.gmap_index),
                                          sense=args.sense,
                                          i=args.isoforms,
                                          o=corrSAM)
                elif args.aligner_choice == "minimap2":
                    print("****Aligning reads with Minimap2...", file=sys.stdout)
                    cmd = MINIMAP2_CMD.format(cpus=args.gmap_threads,
                                              sense=args.sense,
                                              g=args.genome,
                                              i=args.isoforms,
                                              o=corrSAM)
                elif args.aligner_choice == "deSALT":
                    print("****Aligning reads with deSALT...", file=sys.stdout)
                    cmd = DESALT_CMD.format(cpus=args.gmap_threads,
                                            dir=args.gmap_index,
                                            i=args.isoforms,
                                            o=corrSAM)
                if subprocess.check_call(cmd, shell=True)!=0:
                    print("ERROR running alignment cmd: {0}".format(cmd), file=sys.stderr)
                    sys.exit(-1)

            # if is fusion - go in and change the IDs to reflect PBfusion.X.1, PBfusion.X.2...
            if args.is_fusion:
                corrSAM = rewrite_sam_for_fusion_ids(corrSAM)
            # error correct the genome (input: corrSAM, output: corrFASTA)
            err_correct(args.genome, corrSAM, corrFASTA, genome_dict=genome_dict)
            # convert SAM to GFF --> GTF
            convert_sam_to_gff3(corrSAM, corrGTF+'.tmp', source=os.path.basename(args.genome).split('.')[0])  # convert SAM to GFF3
            cmd = "{p} {o}.tmp -T -o {o}".format(o=corrGTF, p=GFFREAD_PROG)
            if subprocess.check_call(cmd, shell=True)!=0:
                print("ERROR running cmd: {0}".format(cmd), file=sys.stderr)
                sys.exit(-1)
        else:
            print("Skipping aligning of sequences because GTF file was provided.", file=sys.stdout)

            ind = 0
            with open(args.isoforms, 'r') as isoforms_gtf:
                for line in isoforms_gtf:
                    if line[0] != "#" and len(line.split("\t"))!=9:
                        sys.stderr.write("\nERROR: input isoforms file with not GTF format.\n")
                        sys.exit()
                    elif len(line.split("\t"))==9:
                        ind += 1
                if ind == 0:
                    print("WARNING: GTF has {0} no annotation lines.".format(args.isoforms), file=sys.stderr)


            # GFF to GTF (in case the user provides gff instead of gtf)
            corrGTF_tpm = corrGTF+".tmp"
            try:
                subprocess.call([GFFREAD_PROG, args.isoforms , '-T', '-o', corrGTF_tpm])
            except (RuntimeError, TypeError, NameError):
                sys.stderr.write('ERROR: File %s without GTF/GFF format.\n' % args.isoforms)
                raise SystemExit(1)


            # check if gtf chromosomes inside genome file
            with open(corrGTF, 'w') as corrGTF_out:
                with open(corrGTF_tpm, 'r') as isoforms_gtf:
                    for line in isoforms_gtf:
                        if line[0] != "#":
                            chrom = line.split("\t")[0]
                            type = line.split("\t")[2]
                            if chrom not in list(genome_dict.keys()):
                                sys.stderr.write("\nERROR: gtf \"%s\" chromosome not found in genome reference file.\n" % (chrom))
                                sys.exit()
                            elif type in ('transcript', 'exon'):
                                corrGTF_out.write(line)
            os.remove(corrGTF_tpm)

            if not os.path.exists(corrSAM):
                sys.stdout.write("\nIndels will be not calculated since you ran SQANTI2 without alignment step (SQANTI2 with gtf format as transcriptome input).\n")

            # GTF to FASTA
            subprocess.call([GFFREAD_PROG, corrGTF, '-g', args.genome, '-w', corrFASTA])

    # ORF generation
    print("**** Predicting ORF sequences...", file=sys.stdout)

    gmst_dir = os.path.join(args.dir, "GMST")
    gmst_pre = os.path.join(gmst_dir, "GMST_tmp")
    if not os.path.exists(gmst_dir):
        os.makedirs(gmst_dir)


    # sequence ID example: PB.2.1 gene_4|GeneMark.hmm|264_aa|+|888|1682
    gmst_rex = re.compile('(\S+\t\S+\|GeneMark.hmm)\|(\d+)_aa\|(\S)\|(\d+)\|(\d+)')
    orfDict = {}  # GMST seq id --> myQueryProteins object
    if args.skipORF:
        print("WARNING: Skipping ORF prediction because user requested it. All isoforms will be non-coding!", file=sys.stderr)
    elif os.path.exists(corrORF):
        print("ORF file {0} already exists. Using it....".format(corrORF), file=sys.stderr)
        for r in SeqIO.parse(open(corrORF), 'fasta'):
            # now process ORFs into myQueryProtein objects
            m = gmst_rex.match(r.description)
            if m is None:
                print("Expected GMST output IDs to be of format '<pbid> gene_4|GeneMark.hmm|<orf>_aa|<strand>|<cds_start>|<cds_end>' but instead saw: {0}! Abort!".format(r.description), file=sys.stderr)
                sys.exit(-1)
            orf_length = int(m.group(2))
            cds_start = int(m.group(4))
            cds_end = int(m.group(5))
            orfDict[r.id] = myQueryProteins(cds_start, cds_end, orf_length, proteinID=r.id)
    else:
        cmd = GMST_CMD.format(i=corrFASTA, o=gmst_pre)
        if subprocess.check_call(cmd, shell=True, cwd=gmst_dir)!=0:
            print("ERROR running GMST cmd: {0}".format(cmd), file=sys.stderr)
            sys.exit(-1)
        # Modifying ORF sequences by removing sequence before ATG
        with open(corrORF, "w") as f:
            for r in SeqIO.parse(open(gmst_pre+'.faa'), 'fasta'):
                m = gmst_rex.match(r.description)
                if m is None:
                    print("Expected GMST output IDs to be of format '<pbid> gene_4|GeneMark.hmm|<orf>_aa|<strand>|<cds_start>|<cds_end>' but instead saw: {0}! Abort!".format(r.description), file=sys.stderr)
                    sys.exit(-1)
                id_pre = m.group(1)
                orf_length = int(m.group(2))
                orf_strand = m.group(3)
                cds_start = int(m.group(4))
                cds_end = int(m.group(5))
                pos = r.seq.find('M')
                if pos!=-1:
                    # must modify both the sequence ID and the sequence
                    orf_length -= pos
                    cds_start += pos*3
                    newid = "{0}|{1}_aa|{2}|{3}|{4}".format(id_pre, orf_length, orf_strand, cds_start, cds_end)
                    newseq = str(r.seq)[pos:]
                    orfDict[r.id] = myQueryProteins(cds_start, cds_end, orf_length, proteinID=newid)
                    f.write(">{0}\n{1}\n".format(newid, newseq))
                else:
                    new_rec = r
                    orfDict[r.id] = myQueryProteins(cds_start, cds_end, orf_length, proteinID=r.id)
                    f.write(">{0}\n{1}\n".format(new_rec.description, new_rec.seq))

    if len(orfDict) == 0:
        print("WARNING: All input isoforms were predicted as non-coding", file=sys.stderr)

    return(orfDict)


def reference_parser(args, genome_chroms):
    """
    Read the reference GTF file
    :param args:
    :param genome_chroms: list of chromosome names from the genome fasta, used for sanity checking
    :return: (refs_1exon_by_chr, refs_exons_by_chr, junctions_by_chr, junctions_by_gene)
    """
    global referenceFiles

    referenceFiles = os.path.join(args.dir, "refAnnotation_"+args.output+".genePred")
    print("**** Parsing Reference Transcriptome....", file=sys.stdout)

    if os.path.exists(referenceFiles):
        print("{0} already exists. Using it.".format(referenceFiles), file=sys.stdout)
    else:
        ## gtf to genePred
        if not args.geneid:
            subprocess.call([GTF2GENEPRED_PROG, args.annotation, referenceFiles, '-genePredExt', '-allErrors', '-ignoreGroupsWithoutExons', '-geneNameAsName2'])
        else:
            subprocess.call([GTF2GENEPRED_PROG, args.annotation, referenceFiles, '-genePredExt', '-allErrors', '-ignoreGroupsWithoutExons'])

    ## parse reference annotation
    # 1. ignore all miRNAs (< 200 bp)
    # 2. separately store single exon and multi-exon references
    refs_1exon_by_chr = defaultdict(lambda: IntervalTree()) #
    refs_exons_by_chr = defaultdict(lambda: IntervalTree())
    # store donors as the exon end (1-based) and acceptor as the exon start (0-based)
    # will convert the sets to sorted list later
    junctions_by_chr = defaultdict(lambda: {'donors': set(), 'acceptors': set(), 'da_pairs': set()})
    # dict of gene name --> set of junctions (don't need to record chromosome)
    junctions_by_gene = defaultdict(lambda: set())
    # dict of gene name --> list of known begins and ends (begin always < end, regardless of strand)
    known_5_3_by_gene = defaultdict(lambda: {'begin':set(), 'end': set()})

    for r in genePredReader(referenceFiles):
        if r.length < 200: continue # ignore miRNAs
        if r.exonCount == 1:
            refs_1exon_by_chr[r.chrom].insert(r.txStart, r.txEnd, r)
            known_5_3_by_gene[r.gene]['begin'].add(r.txStart)
            known_5_3_by_gene[r.gene]['end'].add(r.txEnd)
        else:
            refs_exons_by_chr[r.chrom].insert(r.txStart, r.txEnd, r)
            # only store junctions for multi-exon transcripts
            for d, a in r.junctions:
                junctions_by_chr[r.chrom]['donors'].add(d)
                junctions_by_chr[r.chrom]['acceptors'].add(a)
                junctions_by_chr[r.chrom]['da_pairs'].add((d,a))
                junctions_by_gene[r.gene].add((d,a))
            known_5_3_by_gene[r.gene]['begin'].add(r.txStart)
            known_5_3_by_gene[r.gene]['end'].add(r.txEnd)

    # check that all genes' chromosomes are in the genome file
    ref_chroms = set(refs_1exon_by_chr.keys()).union(list(refs_exons_by_chr.keys()))
    diff = ref_chroms.difference(genome_chroms)
    if len(diff) > 0:
        print("WARNING: ref annotation contains chromosomes not in genome: {0}\n".format(",".join(diff)), file=sys.stderr)

    # convert the content of junctions_by_chr to sorted list
    for k in junctions_by_chr:
        junctions_by_chr[k]['donors'] = list(junctions_by_chr[k]['donors'])
        junctions_by_chr[k]['donors'].sort()
        junctions_by_chr[k]['acceptors'] = list(junctions_by_chr[k]['acceptors'])
        junctions_by_chr[k]['acceptors'].sort()
        junctions_by_chr[k]['da_pairs'] = list(junctions_by_chr[k]['da_pairs'])
        junctions_by_chr[k]['da_pairs'].sort()

    return dict(refs_1exon_by_chr), dict(refs_exons_by_chr), dict(junctions_by_chr), dict(junctions_by_gene), dict(known_5_3_by_gene)


def isoforms_parser(args):
    """
    Parse input isoforms (GTF) to dict (chr --> sorted list)
    """
    global queryFile
    queryFile = os.path.splitext(corrGTF)[0] +".genePred"

    print("**** Parsing Isoforms....", file=sys.stderr)

    # gtf to genePred
    cmd = GTF2GENEPRED_PROG + " {0} {1} -genePredExt -allErrors -ignoreGroupsWithoutExons".format(\
        corrGTF, queryFile)
    if subprocess.check_call(cmd, shell=True)!=0:
        print("ERROR running cmd: {0}".format(cmd), file=sys.stderr)
        sys.exit(-1)


    isoforms_list = defaultdict(lambda: []) # chr --> list to be sorted later

    for r in genePredReader(queryFile):
        isoforms_list[r.chrom].append(r)

    for k in isoforms_list:
        isoforms_list[k].sort(key=lambda r: r.txStart)

    return isoforms_list


def STARcov_parser(coverageFiles): # just valid with unstrand-specific RNA-seq protocols.
    """
    :param coverageFiles: comma-separated list of STAR junction output files or a directory containing junction files
    :return: list of samples, dict of (chrom,strand) --> (0-based start, 1-based end) --> {dict of sample -> unique reads supporting this junction}
    """
    cov_files = glob.glob(coverageFiles)

    print("Input pattern: {0}. The following files found and to be read as junctions:\n{1}".format(\
        coverageFiles, "\n".join(cov_files) ), file=sys.stderr)

    cov_by_chrom_strand = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: 0)))
    undefined_strand_count = 0
    all_read = 0
    samples = []
    for file in cov_files:
        prefix = os.path.basename(file[:file.rfind('.')]) # use this as sample name
        samples.append(prefix)
        for r in STARJunctionReader(file):
            if r.strand == 'NA':
                # undefined strand, so we put them in BOTH strands otherwise we'll lose all non-canonical junctions from STAR
                cov_by_chrom_strand[(r.chrom, '+')][(r.start, r.end)][prefix] = r.unique_count + r.multi_count
                cov_by_chrom_strand[(r.chrom, '-')][(r.start, r.end)][prefix] = r.unique_count + r.multi_count
                undefined_strand_count += 1
            else:
                cov_by_chrom_strand[(r.chrom, r.strand)][(r.start, r.end)][prefix] = r.unique_count + r.multi_count
            all_read += 1
    print("{0} junctions read. {1} junctions added to both strands because no strand information from STAR.".format(all_read, undefined_strand_count), file=sys.stderr)

    return samples, cov_by_chrom_strand

EXP_KALLISTO_HEADERS = ['target_id', 'length', 'eff_length', 'est_counts', 'tpm']
EXP_RSEM_HEADERS = ['transcript_id', 'length', 'effective_length', 'expected_count', 'TPM']
def expression_parser(expressionFile):
    """
    Currently accepts expression format: Kallisto or RSEM
    :param expressionFile: Kallisto or RSEM
    :return: dict of PBID --> TPM
    """
    reader = DictReader(open(expressionFile), delimiter='\t')

    if all(k in reader.fieldnames for k in EXP_KALLISTO_HEADERS):
        print("Detected Kallisto expression format. Using 'target_id' and 'tpm' field.", file=sys.stderr)
        name_id, name_tpm = 'target_id', 'tpm'
    elif all(k in reader.fieldnames for k in EXP_RSEM_HEADERS):
        print("Detected RSEM expression format. Using 'transcript_id' and 'TPM' field.", file=sys.stderr)
        name_id, name_tpm = 'transcript_id', 'TPM'
    else:
        print("Expected Kallisto or RSEM file format from {0}. Abort!".format(expressionFile), file=sys.stderr)

    exp_dict = {}

    for r in reader:
        exp_dict[r[name_id]] = float(r[name_tpm])

    return exp_dict


def transcriptsKnownSpliceSites(refs_1exon_by_chr, refs_exons_by_chr, start_ends_by_gene, trec, genome_dict, nPolyA):
    """
    :param refs_1exon_by_chr: dict of single exon references (chr -> IntervalTree)
    :param refs_exons_by_chr: dict of multi exon references (chr -> IntervalTree)
    :param trec: id record (genePredRecord) to be compared against reference
    :param genome_dict: dict of genome (chrom --> SeqRecord)
    :param nPolyA: window size to look for polyA
    :return: myQueryTranscripts object that indicates the best reference hit
    """
    def get_diff_tss_tts(trec, ref):
        if trec.strand == '+':
            diff_tss = trec.txStart - ref.txStart
            diff_tts = ref.txEnd - trec.txEnd
        else:
            diff_tts = trec.txStart - ref.txStart
            diff_tss = ref.txEnd - trec.txEnd
        return diff_tss, diff_tts


    def get_gene_diff_tss_tts(isoform_hit):
        # now that we know the reference (isoform) it hits
        # add the nearest start/end site for that gene (all isoforms of the gene)
        nearest_start_diff, nearest_end_diff = float('inf'), float('inf')
        for ref_gene in isoform_hit.genes:
            for x in start_ends_by_gene[ref_gene]['begin']:
                d = trec.txStart - x
                if abs(d) < abs(nearest_start_diff):
                    nearest_start_diff = d
            for x in start_ends_by_gene[ref_gene]['end']:
                d = trec.txEnd - x
                if abs(d) < abs(nearest_end_diff):
                    nearest_end_diff = d

        if trec.strand == '+':
            isoform_hit.tss_gene_diff = nearest_start_diff if nearest_start_diff!=float('inf') else 'NA'
            isoform_hit.tts_gene_diff = nearest_end_diff if nearest_end_diff!=float('inf') else 'NA'
        else:
            isoform_hit.tss_gene_diff = -nearest_end_diff if nearest_start_diff!=float('inf') else 'NA'
            isoform_hit.tts_gene_diff = -nearest_start_diff if nearest_end_diff!=float('inf') else 'NA'

    def categorize_incomplete_matches(trec, ref):
        """
        intron_retention --- at least one trec exon covers at least two adjacent ref exons
        complete --- all junctions agree and is not IR
        5prime_fragment --- all junctions agree but trec has less 5' exons
        3prime_fragment --- all junctions agree but trec has less 3' exons
        internal_fragment --- all junctions agree but trec has less 5' and 3' exons
        """
        # check intron retention
        ref_exon_tree = IntervalTree()
        for i,e in enumerate(ref.exons): ref_exon_tree.insert(e.start, e.end, i)
        for e in trec.exons:
            if len(ref_exon_tree.find(e.start, e.end)) > 1: # multiple ref exons covered
                return "intron_retention"

        agree_front = trec.junctions[0]==ref.junctions[0]
        agree_end   = trec.junctions[-1]==ref.junctions[-1]
        if agree_front:
            if agree_end:
                return "complete"
            else: # front agrees, end does not
                return ("3prime_fragment" if trec.strand=='+' else '5prime_fragment')
        else:
            if agree_end: # front does not agree, end agrees
                return ("5prime_fragment" if trec.strand=='+' else '3prime_fragment')
            else:
                return "internal_fragment"

    # Transcript information for a single query id and comparison with reference.

    # Intra-priming: calculate percentage of "A"s right after the end
    if trec.strand == "+":
        pos_TTS = trec.exonEnds[-1]
        seq_downTTS = str(genome_dict[trec.chrom].seq[pos_TTS:pos_TTS+nPolyA]).upper()
    else: # id on - strand
        pos_TTS = trec.exonStarts[0]
        seq_downTTS = str(genome_dict[trec.chrom].seq[pos_TTS-nPolyA:pos_TTS].reverse_complement()).upper()

    percA = float(seq_downTTS.count('A'))/nPolyA*100


    isoform_hit = myQueryTranscripts(id=trec.id, tts_diff="NA", tss_diff="NA",\
                                    num_exons=trec.exonCount,
                                    length=trec.length,
                                    str_class="", \
                                    chrom=trec.chrom,
                                    strand=trec.strand, \
                                    subtype="no_subcategory",\
                                    percAdownTTS=str(percA))

    ##***************************************##
    ########### SPLICED TRANSCRIPTS ###########
    ##***************************************##

    if trec.exonCount >= 2:
        if trec.chrom not in refs_exons_by_chr: return isoform_hit  # return blank hit result
        for ref in refs_exons_by_chr[trec.chrom].find(trec.txStart, trec.txEnd):
            if trec.strand != ref.strand:
                # opposite strand, just record it in AS_genes
                isoform_hit.AS_genes.add(ref.gene)
                continue
            match_type = compare_junctions(trec, ref, internal_fuzzy_max_dist=0, max_5_diff=999999, max_3_diff=999999)

            if match_type not in ('exact', 'subset', 'partial', 'concordant', 'super', 'nomatch'):
                raise Exception("Unknown match category {0}!".format(match_type))

            diff_tss, diff_tts = get_diff_tss_tts(trec, ref)

            # #############################
            # SQANTI's full-splice_match
            # #############################
            if match_type == "exact":
                subtype = "multi-exon"
                if isoform_hit.str_class != 'full-splice_match': # prev hit is not as good as this one, replace it!
                    isoform_hit = myQueryTranscripts(trec.id, diff_tss, diff_tts, trec.exonCount, trec.length,
                                                     str_class="full-splice_match",
                                                     subtype=subtype,
                                                     chrom=trec.chrom,
                                                     strand=trec.strand,
                                                     genes=[ref.gene],
                                                     transcripts=[ref.id],
                                                     refLen = ref.length,
                                                     refExons= ref.exonCount,
                                                     percAdownTTS=str(percA))
                elif abs(diff_tss)+abs(diff_tts) < isoform_hit.get_total_diff(): # prev hit is FSM however this one is better
                    isoform_hit.modify(ref.id, ref.gene, diff_tss, diff_tts, ref.length, ref.exonCount)

            # #######################################################
            # SQANTI's incomplete-splice_match
            # (only check if don't already have a FSM match)
            # #######################################################
            elif isoform_hit.str_class!='full-splice_match' and match_type == "subset":
                subtype = categorize_incomplete_matches(trec, ref)

                if isoform_hit.str_class != 'incomplete-splice_match': # prev hit is not as good as this one, replace it!
                    isoform_hit = myQueryTranscripts(trec.id, diff_tss, diff_tts, trec.exonCount, trec.length,
                                                     str_class="incomplete-splice_match",
                                                     subtype=subtype,
                                                     chrom=trec.chrom,
                                                     strand=trec.strand,
                                                     genes=[ref.gene],
                                                     transcripts=[ref.id],
                                                     refLen = ref.length,
                                                     refExons= ref.exonCount,
                                                     percAdownTTS=str(percA))
                elif abs(diff_tss)+abs(diff_tts) < isoform_hit.get_total_diff():
                    isoform_hit.modify(ref.id, ref.gene, diff_tss, diff_tts, ref.length, ref.exonCount)
                    isoform_hit.subtype = subtype
            # #######################################################
            # Some kind of junction match that isn't ISM/FSM
            # #######################################################
            elif match_type in ('partial', 'concordant', 'super', 'nomatch') and isoform_hit.str_class not in ('full-splice_match', 'incomplete-splice_match'):
                if isoform_hit.str_class=="":
                    isoform_hit = myQueryTranscripts(trec.id, "NA", "NA", trec.exonCount, trec.length,
                                                     str_class="anyKnownSpliceSite",
                                                     subtype="no_subcategory",
                                                     chrom=trec.chrom,
                                                     strand=trec.strand,
                                                     genes=[ref.gene],
                                                     transcripts=["novel"],
                                                     refLen=ref.length,
                                                     refExons=ref.exonCount,
                                                     percAdownTTS=str(percA))
    ##***************************************####
    ########### UNSPLICED TRANSCRIPTS ###########
    ##***************************************####
    else: # single exon id
        if trec.chrom in refs_1exon_by_chr:
            for ref in refs_1exon_by_chr[trec.chrom].find(trec.txStart, trec.txEnd):
                if ref.strand != trec.strand:
                    # opposite strand, just record it in AS_genes
                    isoform_hit.AS_genes.add(ref.gene)
                    continue
                diff_tss, diff_tts = get_diff_tss_tts(trec, ref)

                # see if there's already an existing match AND if so, if this one is better
                if isoform_hit.str_class == "": # no match so far
                    isoform_hit = myQueryTranscripts(trec.id, diff_tss, diff_tts, 1, trec.length, "full-splice_match",
                                                            subtype="mono-exon",
                                                            chrom=trec.chrom,
                                                            strand=trec.strand,
                                                            genes=[ref.gene],
                                                            transcripts=[ref.id],
                                                            refLen=ref.length,
                                                            refExons = ref.exonCount,
                                                            percAdownTTS=str(percA))
                elif abs(diff_tss)+abs(diff_tts) < isoform_hit.get_total_diff():
                    isoform_hit.modify(ref.id, ref.gene, diff_tss, diff_tts, ref.length, ref.exonCount)

        if isoform_hit.str_class == "" and trec.chrom in refs_exons_by_chr:
            # no hits to single exon genes, let's see if it hits multi-exon genes
            # (1) if it overlaps with a ref exon and is contained in an exon, we call it ISM
            # (2) else, if it is completely within a ref gene start-end region, we call it NIC by intron retention
            for ref in refs_exons_by_chr[trec.chrom].find(trec.txStart, trec.txEnd):
                if ref.strand != trec.strand:
                    # opposite strand, just record it in AS_genes
                    isoform_hit.AS_genes.add(ref.gene)
                    continue
                diff_tss, diff_tts = get_diff_tss_tts(trec, ref)

                for e in ref.exons:
                    if e.start <= trec.txStart < trec.txEnd <= e.end:
                        isoform_hit.str_class = "incomplete-splice_match"
                        isoform_hit.subtype = "mono-exon"
                        isoform_hit.modify(ref.id, ref.gene, diff_tss, diff_tts, ref.length, ref.exonCount)
                        # this is as good a match as it gets, we can stop the search here
                        get_gene_diff_tss_tts(isoform_hit)
                        return isoform_hit

                # if we haven't exited here, then ISM hit is not found yet
                # instead check if it's NIC by intron retention
                # but we don't exit here since the next gene could be a ISM hit
                if ref.txStart <= trec.txStart < trec.txEnd <= ref.txEnd:
                    isoform_hit.str_class = "novel_in_catalog"
                    isoform_hit.subtype = "mono-exon"
                    # check for intron retention
                    if len(ref.junctions) > 0:
                        for (d,a) in ref.junctions:
                            if trec.txStart < d < a < trec.txEnd:
                                isoform_hit.subtype = "mono-exon_by_intron_retention"
                                break
                    isoform_hit.modify("novel", ref.gene, 'NA', 'NA', ref.length, ref.exonCount)
                    get_gene_diff_tss_tts(isoform_hit)
                    return isoform_hit

                # if we get to here, means neither ISM nor NIC, so just add a ref gene and categorize further later
                isoform_hit.genes.append(ref.gene)

    get_gene_diff_tss_tts(isoform_hit)
    return isoform_hit


def novelIsoformsKnownGenes(isoforms_hit, trec, junctions_by_chr, junctions_by_gene):
    """
    At this point: definitely not FSM or ISM, see if it is NIC, NNC, or fusion
    :return isoforms_hit: updated isoforms hit (myQueryTranscripts object)
    """
    def has_intron_retention():
        for e in trec.exons:
            m = bisect.bisect_left(junctions_by_chr[trec.chrom]['da_pairs'], (e.start, e.end))
            if m < len(junctions_by_chr[trec.chrom]['da_pairs']) and e.start <= junctions_by_chr[trec.chrom]['da_pairs'][m][0] < junctions_by_chr[trec.chrom]['da_pairs'][m][1] < e.end:
                return True
        return False

    ref_genes = list(set(isoforms_hit.genes))

    #
    # at this point, we have already found matching genes/transcripts
    # hence we do not need to update refLen or refExon
    # or tss_diff and tts_diff (always set to "NA" for non-FSM/ISM matches)
    #
    isoforms_hit.transcripts = ["novel"]
    if len(ref_genes) == 1:
        # hits exactly one gene, must be either NIC or NNC
        ref_gene_junctions = junctions_by_gene[ref_genes[0]]
        # 1. check if all donors/acceptor sites are known (regardless of which ref gene it came from)
        # 2. check if this query isoform uses a subset of the junctions from the single ref hit
        all_junctions_known = True
        all_junctions_in_hit_ref = True
        for d,a in trec.junctions:
            all_junctions_known = all_junctions_known and (d in junctions_by_chr[trec.chrom]['donors']) and (a in junctions_by_chr[trec.chrom]['acceptors'])
            all_junctions_in_hit_ref = all_junctions_in_hit_ref and ((d,a) in ref_gene_junctions)
        if all_junctions_known:
            isoforms_hit.str_class="novel_in_catalog"
            if all_junctions_in_hit_ref:
                isoforms_hit.subtype = "combination_of_known_junctions"
            else:
                isoforms_hit.subtype = "combination_of_known_splicesites"
        else:
            isoforms_hit.str_class="novel_not_in_catalog"
            isoforms_hit.subtype = "at_least_one_novel_splicesite"
    else: # see if it is fusion
        # list of a ref junctions from all genes, including potential shared junctions
        all_ref_junctions = list(itertools.chain(junctions_by_gene[ref_gene] for ref_gene in ref_genes))

        # (junction index) --> number of refs that have this junction
        junction_ref_hit = dict((i, all_ref_junctions.count(junc)) for i,junc in enumerate(trec.junctions))

        # if the same query junction appears in more than one of the hit references, it is not a fusion
        if max(junction_ref_hit.values()) > 1:
            isoforms_hit.str_class = "moreJunctions"
        else:
            isoforms_hit.str_class = "fusion"
            isoforms_hit.subtype = "mono-exon" if trec.exonCount==1 else "multi-exon"

    if has_intron_retention():
        isoforms_hit.subtype = "intron_retention"

    return isoforms_hit

def associationOverlapping(isoforms_hit, trec, junctions_by_chr):

    # at this point: definitely not FSM or ISM or NIC
    # possibly (in order of preference assignment):
    #  - NNC  (multi-exon and overlaps some ref on same strand, dun care if junctions are known)
    #  - antisense  (on opp strand of a known gene)
    #  - genic (overlaps a combination of exons and introns), ignore strand
    #  - genic_intron  (completely within an intron), ignore strand
    #  - intergenic (does not overlap a gene at all), ignore strand

    isoforms_hit.str_class = "intergenic"
    isoforms_hit.transcripts = ["novel"]
    isoforms_hit.subtype = "mono-exon" if trec.exonCount==1 else "multi-exon"

    if len(isoforms_hit.genes) == 0:
        # completely no overlap with any genes on the same strand
        # check if it is anti-sense to a known gene, otherwise it's genic_intron or intergenic
        if len(isoforms_hit.AS_genes) == 0 and trec.chrom in junctions_by_chr:
            # no hit even on opp strand
            # see if it is completely contained within a junction
            da_pairs = junctions_by_chr[trec.chrom]['da_pairs']
            i = bisect.bisect_left(da_pairs, (trec.txStart, trec.txEnd))
            while i < len(da_pairs) and da_pairs[i][0] <= trec.txStart:
                if da_pairs[i][0] <= trec.txStart <= trec.txStart <= da_pairs[i][1]:
                    isoforms_hit.str_class = "genic_intron"
                    break
                i += 1
        else:
            # hits one or more genes on the opposite strand
            isoforms_hit.str_class = "antisense"
            isoforms_hit.genes = ["novelGene_{g}_AS".format(g=g) for g in isoforms_hit.AS_genes]
    else:
        # overlaps with one or more genes on the same strand
        if trec.exonCount >= 2:
            # multi-exon and has a same strand gene hit, must be NNC
            isoforms_hit.str_class = "novel_not_in_catalog"
            isoforms_hit.subtype = "at_least_one_novel_splicesite"
        else:
            # single exon, must be genic
            isoforms_hit.str_class = "genic"

    return isoforms_hit


def write_junctionInfo(trec, junctions_by_chr, accepted_canonical_sites, indelInfo, genome_dict, fout, covInf=None, covNames=None, phyloP_reader=None):
    """
    :param trec: query isoform genePredRecord
    :param junctions_by_chr: dict of chr -> {'donors': <sorted list of donors>, 'acceptors': <sorted list of acceptors>, 'da_pairs': <sorted list of junctions>}
    :param accepted_canonical_sites: list of accepted canonical splice sites
    :param indelInfo: indels near junction information, dict of pbid --> list of junctions near indel (in Interval format)
    :param genome_dict: genome fasta dict
    :param fout: DictWriter handle
    :param covInf: (optional) junction coverage information, dict of (chrom,strand) -> (0-based start,1-based end) -> dict of {sample -> unique read count}
    :param covNames: (optional) list of sample names for the junction coverage information
    :param phyloP_reader: (optional) dict of (chrom,0-based coord) --> phyloP score

    Write a record for each junction in query isoform
    """
    def find_closest_in_list(lst, pos):
        i = bisect.bisect_left(lst, pos)
        if i == 0:
            return lst[0]-pos
        elif i == len(lst):
            return lst[-1]-pos
        else:
            a, b = lst[i-1]-pos, lst[i]-pos
            if abs(a) < abs(b): return a
            else: return b

    if trec.chrom not in junctions_by_chr:
        # nothing to do
        return

    # go through each trec junction
    for junction_index, (d, a) in enumerate(trec.junctions):
        # NOTE: donor just means the start, not adjusted for strand
        # find the closest junction start site
        min_diff_s = -find_closest_in_list(junctions_by_chr[trec.chrom]['donors'], d)
        # find the closest junction end site
        min_diff_e = find_closest_in_list(junctions_by_chr[trec.chrom]['acceptors'], a)

        splice_site = trec.get_splice_site(genome_dict, junction_index)

        indel_near_junction = "NA"
        if indelInfo is not None:
            indel_near_junction = "TRUE" if (trec.id in indelInfo and Interval(d,a) in indelInfo[trec.id]) else "FALSE"

        sample_cov = defaultdict(lambda: 0)  # sample -> unique count for this junction
        if covInf is not None:
            sample_cov = covInf[(trec.chrom, trec.strand)][(d,a)]

        # if phyloP score dict exists, give the triplet score of (last base in donor exon), donor site -- similarly for acceptor
        phyloP_start, phyloP_end = 'NA', 'NA'
        if phyloP_reader is not None:
            phyloP_start = ",".join([str(x) for x in [phyloP_reader.get_pos(trec.chrom, d-1), phyloP_reader.get_pos(trec.chrom, d), phyloP_reader.get_pos(trec.chrom, d+1)]])
            phyloP_end = ",".join([str(x) for x in [phyloP_reader.get_pos(trec.chrom, a-1), phyloP_reader.get_pos(trec.chrom, a),
                                              phyloP_reader.get_pos(trec.chrom, a+1)]])

        qj = {'isoform': trec.id,
              'junction_number': "junction_"+str(junction_index+1),
              "chrom": trec.chrom,
              "strand": trec.strand,
              "genomic_start_coord": d+1,  # write out as 1-based start
              "genomic_end_coord": a,      # already is 1-based end
              "transcript_coord": "?????",  # this is where the exon ends w.r.t to id sequence, ToDo: implement later
              "junction_category": "known" if ((d,a) in junctions_by_chr[trec.chrom]['da_pairs']) else "novel",
              "start_site_category": "known" if min_diff_s==0 else "novel",
              "end_site_category": "known" if min_diff_e==0 else "novel",
              "diff_to_Ref_start_site": min_diff_s,
              "diff_to_Ref_end_site": min_diff_e,
              "bite_junction": "TRUE" if (min_diff_s==0 or min_diff_e==0) else "FALSE",
              "splice_site": splice_site,
              "canonical": "canonical" if splice_site in accepted_canonical_sites else "non_canonical",
              "RTS_junction": "????", # First write ???? in _tmp, later is TRUE/FALSE
              "indel_near_junct": indel_near_junction,
              "phyloP_start": phyloP_start,
              "phyloP_end": phyloP_end,
              "sample_with_cov": sum(cov!=0 for cov in sample_cov.values()) if covInf is not None else "NA",
              "total_coverage": sum(sample_cov.values()) if covInf is not None else "NA"}

        if covInf is not None:
            for sample in covNames:
                qj[sample] = sample_cov[sample]

        fout.writerow(qj)


def isoformClassification(args, isoforms_by_chr, refs_1exon_by_chr, refs_exons_by_chr, junctions_by_chr, junctions_by_gene, start_ends_by_gene, genome_dict, indelsJunc, orfDict):

    ## read coverage files if provided

    if args.coverage is not None:
        print("**** Reading Splice Junctions coverage files.", file=sys.stdout)
        SJcovNames, SJcovInfo = STARcov_parser(args.coverage)
        fields_junc_cur = FIELDS_JUNC + SJcovNames # add the samples to the header
    else:
        SJcovNames, SJcovInfo = None, None
        print("Splice Junction Coverage files not provided.", file=sys.stdout)
        fields_junc_cur = FIELDS_JUNC

    if args.cage_peak is not None:
        print("**** Reading CAGE Peak data.", file=sys.stdout)
        cage_peak_obj = CAGEPeak(args.cage_peak)
    else:
        cage_peak_obj = None


    if args.polyA_motif_list is not None:
        print("**** Reading PolyA motif list.", file=sys.stdout)
        polyA_motif_list = []
        for line in open(args.polyA_motif_list):
            x = line.strip().upper().replace('U', 'A')
            if any(s not in ('A','T','C','G') for s in x):
                print("PolyA motif must be A/T/C/G only! Saw: {0}. Abort!".format(x), file=sys.stderr)
                sys.exit(-1)
            polyA_motif_list.append(x)
    else:
        polyA_motif_list = None


    if args.phyloP_bed is not None:
        print("**** Reading PhyloP BED file.", file=sys.stdout)
        phyloP_reader = LazyBEDPointReader(args.phyloP_bed)
    else:
        phyloP_reader = None

    # running classification
    print("**** Performing Classification of Isoforms....", file=sys.stdout)


    accepted_canonical_sites = list(args.sites.split(","))

    outputPathPrefix = args.dir+"/"+args.output

    outputClassPath = outputPathPrefix+"_classification.txt"
    fout_class = DictWriter(open(outputClassPath+"_tmp", "w"), fieldnames=FIELDS_CLASS, delimiter='\t')
    fout_class.writeheader()

    outputJuncPath = outputPathPrefix+"_junctions.txt"
    fout_junc = DictWriter(open(outputJuncPath+"_tmp", "w"), fieldnames=fields_junc_cur, delimiter='\t')
    fout_junc.writeheader()

    isoforms_info = {}
    novel_gene_index = 1

    for chrom,records in isoforms_by_chr.items():
        for rec in records:
            # Find best reference hit
            isoform_hit = transcriptsKnownSpliceSites(refs_1exon_by_chr, refs_exons_by_chr, start_ends_by_gene, rec, genome_dict, nPolyA=args.window)

            if isoform_hit.str_class == "anyKnownSpliceSite":
                # not FSM or ISM --> see if it is NIC, NNC, or fusion
                isoform_hit = novelIsoformsKnownGenes(isoform_hit, rec, junctions_by_chr, junctions_by_gene)
            elif isoform_hit.str_class == "":
                # possibly NNC, genic, genic intron, anti-sense, or intergenic
                isoform_hit = associationOverlapping(isoform_hit, rec, junctions_by_chr)

            # write out junction information
            write_junctionInfo(rec, junctions_by_chr, accepted_canonical_sites, indelsJunc, genome_dict, fout_junc, covInf=SJcovInfo, covNames=SJcovNames, phyloP_reader=phyloP_reader)

            if isoform_hit.str_class in ("intergenic", "genic_intron"):
                # Liz: I don't find it necessary to cluster these novel genes. They should already be always non-overlapping.
                isoform_hit.genes = ['novelGene_' + str(novel_gene_index)]
                isoform_hit.transcripts = ['novel']
                novel_gene_index += 1

            # look at Cage Peak info (if available)
            if cage_peak_obj is not None:
                if rec.strand == '+':
                    within_cage, dist_cage = cage_peak_obj.find(rec.chrom, rec.strand, rec.txStart)
                else:
                    within_cage, dist_cage = cage_peak_obj.find(rec.chrom, rec.strand, rec.txEnd)
                isoform_hit.within_cage = within_cage
                isoform_hit.dist_cage = dist_cage

            # polyA motif finding: look within 50 bp upstream of 3' end for the highest ranking polyA motif signal (user provided)
            if polyA_motif_list is not None:
                if rec.strand == '+':
                    polyA_motif, polyA_dist = find_polyA_motif(str(genome_dict[rec.chrom][rec.txEnd-50:rec.txEnd].seq), polyA_motif_list)
                else:
                    polyA_motif, polyA_dist = find_polyA_motif(str(genome_dict[rec.chrom][rec.txStart:rec.txStart+50].reverse_complement().seq), polyA_motif_list)
                isoform_hit.polyA_motif = polyA_motif
                isoform_hit.polyA_dist = polyA_dist

            # Fill in ORF/coding info and NMD detection
            if rec.id in orfDict:
                isoform_hit.coding = "coding"
                isoform_hit.ORFlen = orfDict[rec.id].orf_length
                isoform_hit.CDS_start = orfDict[rec.id].cds_start  # 1-based start
                isoform_hit.CDS_end = orfDict[rec.id].cds_end      # 1-based end

                m = {} # transcript coord (0-based) --> genomic coord (0-based)
                if rec.strand == '+':
                    i = 0
                    for exon in rec.exons:
                        for c in range(exon.start, exon.end):
                            m[i] = c
                            i += 1
                else: # - strand
                    i = 0
                    for exon in rec.exons:
                        for c in range(exon.start, exon.end):
                            m[rec.length-i-1] = c
                            i += 1

                orfDict[rec.id].cds_genomic_start = m[orfDict[rec.id].cds_start-1] + 1  # make it 1-based
                orfDict[rec.id].cds_genomic_end   = m[orfDict[rec.id].cds_end-1] + 1    # make it 1-based

                isoform_hit.CDS_genomic_start = orfDict[rec.id].cds_genomic_start
                isoform_hit.CDS_genomic_end = orfDict[rec.id].cds_genomic_end
                if orfDict[rec.id].cds_genomic_start is None: # likely SAM CIGAR mapping issue coming from aligner
                    continue # we have to skip the NMD
                # NMD detection
                # if + strand, see if CDS stop is before the last junction
                if len(rec.junctions) > 0:
                    if rec.strand == '+':
                        dist_to_last_junc = orfDict[rec.id].cds_genomic_end - rec.junctions[-1][0]
                    else: # - strand
                        dist_to_last_junc = rec.junctions[0][1] - orfDict[rec.id].cds_genomic_end
                    isoform_hit.is_NMD = "TRUE" if dist_to_last_junc < 0 else "FALSE"

            isoforms_info[rec.id] = isoform_hit
            fout_class.writerow(isoform_hit.as_dict())

    return isoforms_info


def pstdev(data):
    """Calculates the population standard deviation."""
    n = len(data)
    mean = sum(data)*1. / n  # mean
    var = sum(pow(x - mean, 2) for x in data) / n  # variance
    return math.sqrt(var)  # standard deviation


def find_polyA_motif(genome_seq, polyA_motif_list):
    """
    :param genome_seq: genomic sequence to search polyA motifs from, must already be oriented
    :param polyA_motif_list: ranked list of motifs to find, report the top one found
    :return: polyA_motif, polyA_dist (how many bases upstream is this found)
    """
    for motif in polyA_motif_list:
        i = genome_seq.find(motif)
        if i >= 0:
            return motif, -(len(genome_seq)-i-len(motif)+1)
    return 'NA', 'NA'

def FLcount_parser(fl_count_filename):
    """
    :param fl_count_filename: could be a single sample or multi-sample (chained or demux) count file
    :return: list of samples, <dict>

    If single sample, returns True, dict of {pbid} -> {count}
    If multiple sample, returns False, dict of {pbid} -> {sample} -> {count}

    For multi-sample, acceptable formats are:
    //demux-based
    id,JL3N,FL1N,CL1N,FL3N,CL3N,JL1N
    PB.2.1,0,0,1,0,0,1
    PB.3.3,33,14,47,24,15,38
    PB.3.2,2,1,0,0,0,1

    //chain-based
    superPBID<tab>sample1<tab>sample2
    """
    fl_count_dict = {}
    samples = ['NA']
    flag_single_sample = True

    f = open(fl_count_filename)
    while True:
        cur_pos = f.tell()
        line = f.readline()
        if not line.startswith('#'):
            # if it first thing is superPBID or id or pbid
            if line.startswith('pbid'):
                type = 'SINGLE_SAMPLE'
                sep  = '\t'
            elif line.startswith('superPBID'):
                type = 'MULTI_CHAIN'
                sep = '\t'
            elif line.startswith('id'):
                type = 'MULTI_DEMUX'
                sep = ','
            else:
                raise Exception("Unexpected count file format! Abort!")
            f.seek(cur_pos)
            break


    reader = DictReader(f, delimiter=sep)
    count_header = reader.fieldnames
    if type=='SINGLE_SAMPLE':
        if 'count_fl' not in count_header:
            print("Expected `count_fl` field in count file {0}. Abort!".format(fl_count_filename), file=sys.stderr)
            sys.exit(-1)
        d = dict((r['pbid'], r) for r in reader)
    elif type=='MULTI_CHAIN':
        d = dict((r['superPBID'], r) for r in reader)
        flag_single_sample = False
    elif type=='MULTI_DEMUX':
        d = dict((r['id'], r) for r in reader)
        flag_single_sample = False
    else:
        print("Expected pbid or superPBID as a column in count file {0}. Abort!".format(fl_count_filename), file=sys.stderr)
        sys.exit(-1)
    f.close()


    if flag_single_sample: # single sample
        for k,v in d.items():
            fl_count_dict[k] = int(v['count_fl'])
    else: # multi-sample
        for k,v in d.items():
            fl_count_dict[k] = {}
            samples = list(v.keys())
            for sample,count in v.items():
                if sample not in ('superPBID', 'id'):
                    fl_count_dict[k][sample] = int(count) if count!='NA' else 0

    samples.sort()

    if type=='MULTI_CHAIN':
        samples.remove('superPBID')
    elif type=='MULTI_DEMUX':
        samples.remove('id')

    return samples, fl_count_dict

def run(args):

    start3 = timeit.default_timer()

    print("**** Parsing provided files....", file=sys.stdout)

    print("Reading genome fasta {0}....".format(args.genome), file=sys.stdout)
    # NOTE: can't use LazyFastaReader because inefficient. Bring the whole genome in!
    genome_dict = dict((r.name, r) for r in SeqIO.parse(open(args.genome), 'fasta'))

    ## correction of sequences and ORF prediction (if gtf provided instead of fasta file, correction of sequences will be skipped)
    orfDict = correctionPlusORFpred(args, genome_dict)

    ## parse reference id (GTF) to dicts
    refs_1exon_by_chr, refs_exons_by_chr, junctions_by_chr, junctions_by_gene, start_ends_by_gene = reference_parser(args, list(genome_dict.keys()))

    ## parse query isoforms
    isoforms_by_chr = isoforms_parser(args)

    ## Run indel computation if sam exists
    # indelsJunc: dict of pbid --> list of junctions near indel (in Interval format)
    # indelsTotal: dict of pbid --> total indels count
    if os.path.exists(corrSAM):
        (indelsJunc, indelsTotal) = calc_indels_from_sam(corrSAM)
    else:
        indelsJunc = None
        indelsTotal = None

    # isoform classification + intra-priming + id and junction characterization
    isoforms_info = isoformClassification(args, isoforms_by_chr, refs_1exon_by_chr, refs_exons_by_chr, junctions_by_chr, junctions_by_gene, start_ends_by_gene, genome_dict, indelsJunc, orfDict)

    print("Number of classified isoforms: {0}".format(len(isoforms_info)), file=sys.stdout)

    write_collapsed_GFF_with_CDS(isoforms_info, corrGTF, corrGTF+'.cds.gff')

    outputPathPrefix = os.path.join(args.dir, args.output)
    outputClassPath = outputPathPrefix + "_classification.txt"
    outputJuncPath = outputPathPrefix + "_junctions.txt"

    ## RT-switching computation
    print("**** RT-switching computation....", file=sys.stderr)

    # RTS_info: dict of (pbid) -> list of RT junction. if RTS_info[pbid] == [], means all junctions are non-RT.
    RTS_info = rts([outputJuncPath+"_tmp", args.genome, "-a"], genome_dict)
    for pbid in isoforms_info:
        if pbid in RTS_info and len(RTS_info[pbid]) > 0:
            isoforms_info[pbid].RT_switching = "TRUE"
        else:
            isoforms_info[pbid].RT_switching = "FALSE"


    ## FSM classification
    geneFSM_dict = defaultdict(lambda: [])
    for iso in isoforms_info:
        gene = isoforms_info[iso].geneName()  # if multi-gene, returns "geneA_geneB_geneC..."
        geneFSM_dict[gene].append(isoforms_info[iso].str_class)

    fields_class_cur = FIELDS_CLASS
    ## FL count file
    if args.fl_count:
        if not os.path.exists(args.fl_count):
            print("FL count file {0} does not exist!".format(args.fl_count), file=sys.stderr)
            sys.exit(-1)
        print("**** Reading Full-length read abundance files...", file=sys.stderr)
        fl_samples, fl_count_dict = FLcount_parser(args.fl_count)
        for pbid in fl_count_dict:
            if pbid not in isoforms_info:
                print("WARNING: {0} found in FL count file but not in input fasta.".format(pbid), file=sys.stderr)
        if len(fl_samples) == 1: # single sample from PacBio
            print("Single-sample PacBio FL count format detected.", file=sys.stderr)
            for iso in isoforms_info:
                if iso in fl_count_dict:
                    isoforms_info[iso].FL = fl_count_dict[iso]
                else:
                    print("WARNING: {0} not found in FL count file. Assign count as 0.".format(iso), file=sys.stderr)
                    isoforms_info[iso].FL = 0
        else: # multi-sample
            print("Multi-sample PacBio FL count format detected.", file=sys.stderr)
            fields_class_cur = FIELDS_CLASS + ["FL."+s for s in fl_samples]
            for iso in isoforms_info:
                if iso in fl_count_dict:
                    isoforms_info[iso].FL_dict = fl_count_dict[iso]
                else:
                    print("WARNING: {0} not found in FL count file. Assign count as 0.".format(iso), file=sys.stderr)
                    isoforms_info[iso].FL_dict = defaultdict(lambda: 0)
    else:
        print("Full-length read abundance files not provided.", file=sys.stderr)


    ## Isoform expression information
    if args.expression:
        print("**** Reading Isoform Expression Information.", file=sys.stderr)
        exp_dict = expression_parser(args.expression)
        gene_exp_dict = {}
        for iso in isoforms_info:
            if iso not in exp_dict:
                exp_dict[iso] = 0
                print("WARNING: isoform {0} not found in expression matrix. Assigning TPM of 0.".format(iso), file=sys.stderr)
            gene = isoforms_info[iso].geneName()
            if gene not in gene_exp_dict:
                gene_exp_dict[gene] = exp_dict[iso]
            else:
                gene_exp_dict[gene] = gene_exp_dict[gene]+exp_dict[iso]
    else:
        exp_dict = None
        gene_exp_dict = None
        print("Isoforms expression files not provided.", file=sys.stderr)


    ## Adding indel, FSM class and expression information
    for iso in isoforms_info:
        gene = isoforms_info[iso].geneName()
        if exp_dict is not None and gene_exp_dict is not None:
            isoforms_info[iso].geneExp = gene_exp_dict[gene]
            isoforms_info[iso].isoExp  = exp_dict[iso]
        if len(geneFSM_dict[gene])==1:
            isoforms_info[iso].FSM_class = "A"
        elif "full-splice_match" in geneFSM_dict[gene]:
            isoforms_info[iso].FSM_class = "C"
        else:
            isoforms_info[iso].FSM_class = "B"

    if indelsTotal is not None:
        for iso in isoforms_info:
            if iso in indelsTotal:
                isoforms_info[iso].nIndels = indelsTotal[iso]
            else:
                isoforms_info[iso].nIndels = 0


    ## Read junction files and create attributes per id
    # Read the junction information to fill in several remaining unfilled fields in classification
    # (1) "canonical": is "canonical" if all junctions are canonical, otherwise "non_canonical"
    # (2) "bite": is TRUE if any of the junction "bite_junction" field is TRUE

    reader = DictReader(open(outputJuncPath+"_tmp"), delimiter='\t')
    fields_junc_cur = reader.fieldnames
    sj_covs_by_isoform = defaultdict(lambda: [])  # pbid --> list of total_cov for each junction so we can calculate SD later
    for r in reader:
        # only need to do assignment if:
        # (1) the .canonical field is still "NA"
        # (2) the junction is non-canonical
        assert r['canonical'] in ('canonical', 'non_canonical')
        if (isoforms_info[r['isoform']].canonical == 'NA') or \
            (r['canonical'] == 'non_canonical'):
            isoforms_info[r['isoform']].canonical = r['canonical']

        if (isoforms_info[r['isoform']].bite == 'NA') or (r['bite_junction'] == 'TRUE'):
            isoforms_info[r['isoform']].bite = r['bite_junction']

        if r['indel_near_junct'] == 'TRUE':
            if isoforms_info[r['isoform']].nIndelsJunc == 'NA':
                isoforms_info[r['isoform']].nIndelsJunc = 0
            isoforms_info[r['isoform']].nIndelsJunc += 1

        # min_cov: min( total_cov[j] for each junction j in this isoform )
        # min_cov_pos: the junction [j] that attributed to argmin(total_cov[j])
        # min_sample_cov: min( sample_cov[j] for each junction in this isoform )
        # sd_cov: sd( total_cov[j] for each junction j in this isoform )
        if r['sample_with_cov'] != 'NA':
            sample_with_cov = int(r['sample_with_cov'])
            if (isoforms_info[r['isoform']].min_samp_cov == 'NA') or (isoforms_info[r['isoform']].min_samp_cov > sample_with_cov):
                isoforms_info[r['isoform']].min_samp_cov = sample_with_cov

        if r['total_coverage'] != 'NA':
            total_cov = int(r['total_coverage'])
            sj_covs_by_isoform[r['isoform']].append(total_cov)
            if (isoforms_info[r['isoform']].min_cov == 'NA') or (isoforms_info[r['isoform']].min_cov > total_cov):
                isoforms_info[r['isoform']].min_cov = total_cov
                isoforms_info[r['isoform']].min_cov_pos = r['junction_number']


    for pbid, covs in sj_covs_by_isoform.items():
        isoforms_info[pbid].sd = pstdev(covs)

    #### Printing output file:

    print("**** Writing output files....", file=sys.stderr)

    # sort isoform keys
    iso_keys = list(isoforms_info.keys())
    iso_keys.sort(key=lambda x: (isoforms_info[x].chrom,isoforms_info[x].id))
    with open(outputClassPath, 'w') as h:
        fout_class = DictWriter(h, fieldnames=fields_class_cur, delimiter='\t')
        fout_class.writeheader()
        for iso_key in iso_keys:
            fout_class.writerow(isoforms_info[iso_key].as_dict())

    # Now that RTS info is obtained, we can write the final junctions.txt
    with open(outputJuncPath, 'w') as h:
        fout_junc = DictWriter(h, fieldnames=fields_junc_cur, delimiter='\t')
        fout_junc.writeheader()
        for r in DictReader(open(outputJuncPath+"_tmp"), delimiter='\t'):
            if r['isoform'] in RTS_info:
                if r['junction_number'] in RTS_info[r['isoform']]:
                    r['RTS_junction'] = 'TRUE'
                else:
                    r['RTS_junction'] = 'FALSE'
            fout_junc.writerow(r)

    ## Generating report

    print("**** Generating SQANTI report....", file=sys.stderr)
    cmd = RSCRIPTPATH + " {d}/{f} {c} {j}".format(d=utilitiesPath, f=RSCRIPT_REPORT, c=outputClassPath, j=outputJuncPath)
    if subprocess.check_call(cmd, shell=True)!=0:
        print("ERROR running command: {0}".format(cmd), file=sys.stderr)
        sys.exit(-1)
    stop3 = timeit.default_timer()

    print("Removing temporary files....", file=sys.stderr)
    os.remove(outputClassPath+"_tmp")
    os.remove(outputJuncPath+"_tmp")


    print("SQANTI complete in {0} sec.".format(stop3 - start3), file=sys.stderr)


def rename_isoform_seqids(input_fasta, force_id_ignore=False):
    """
    Rename input isoform fasta/fastq, which is usually mapped, collapsed Iso-Seq data with IDs like:

    PB.1.1|chr1:10-100|xxxxxx

    to just being "PB.1.1"

    :param input_fasta: Could be either fasta or fastq, autodetect.
    :return: output fasta with the cleaned up sequence ID, is_fusion flag
    """
    type = 'fasta'
    with open(input_fasta) as h:
        if h.readline().startswith('@'): type = 'fastq'
    f = open(input_fasta[:input_fasta.rfind('.')]+'.renamed.fasta', 'w')
    for r in SeqIO.parse(open(input_fasta), type):
        m1 = seqid_rex1.match(r.id)
        m2 = seqid_rex2.match(r.id)
        m3 = seqid_fusion.match(r.id)
        if not force_id_ignore and (m1 is None and m2 is None and m3 is None):
            print("Invalid input IDs! Expected PB.X.Y or PB.X.Y|xxxxx or PBfusion.X format but saw {0} instead. Abort!".format(r.id), file=sys.stderr)
            sys.exit(-1)
        if r.id.startswith('PB.') or r.id.startswith('PBfusion.'):  # PacBio fasta header
            newid = r.id.split('|')[0]
        else:
            raw = r.id.split('|')
            if len(raw) > 4:  # RefSeq fasta header
                newid = raw[3]
            else:
                newid = r.id.split()[0]  # Ensembl fasta header
        f.write(">{0}\n{1}\n".format(newid, r.seq))
    f.close()
    return f.name


class CAGEPeak:
    def __init__(self, cage_bed_filename):
        self.cage_bed_filename = cage_bed_filename
        self.cage_peaks = defaultdict(lambda: IntervalTree()) # (chrom,strand) --> intervals of peaks

        self.read_bed()

    def read_bed(self):
        for line in open(self.cage_bed_filename):
            raw = line.strip().split()
            chrom = raw[0]
            start0 = int(raw[1])
            end1 = int(raw[2])
            strand = raw[5]
            tss0 = int(raw[6])
            self.cage_peaks[(chrom,strand)].insert(start0, end1, (tss0, start0, end1))

    def find(self, chrom, strand, query, search_window=10000):
        """
        :param start0: 0-based start of the 5' end to query
        :return: <True/False falls within a cage peak>, <nearest dist to TSS>
        dist to TSS is 0 if right on spot
        dist to TSS is + if downstream, - if upstream (watch for strand!!!)
        """
        within_peak, dist_peak = False, 'NA'
        for (tss0,start0,end1) in self.cage_peaks[(chrom,strand)].find(query-search_window, query+search_window):
            if not within_peak:
                within_peak, dist_peak = (start0<=query<end1), (query - tss0) * (-1 if strand=='-' else +1)
            else:
                d = (query - tss0) * (-1 if strand=='-' else +1)
                if abs(d) < abs(dist_peak):
                    within_peak, dist_peak = (start0<=query<end1), d
        return within_peak, dist_peak



def main():

    global utilitiesPath

    #arguments
    parser = argparse.ArgumentParser(description="Structural and Quality Annotation of Novel Transcript Isoforms")
    parser.add_argument('isoforms', help='\tIsoforms (FASTA/FASTQ or gtf format; By default "FASTA/FASTQ". GTF if specified -g option)')
    parser.add_argument('annotation', help='\t\tReference annotation file (GTF format)')
    parser.add_argument('genome', help='\t\tReference genome (Fasta format)')
    parser.add_argument("--force_id_ignore", action="store_true", default=False, help=argparse.SUPPRESS)
    parser.add_argument("--aligner_choice", choices=['minimap2', 'deSALT', 'gmap'], default='minimap2')
    parser.add_argument('--cage_peak', help='\t\tFANTOM5 Cage Peak (BED format, optional)')
    parser.add_argument("--polyA_motif_list", help="\t\tRanked list of polyA motifs (text, optional)")
    parser.add_argument("--phyloP_bed", help="\t\tPhyloP BED for conservation score (BED, optional)")
    parser.add_argument("--skipORF", default=False, action="store_true", help="\t\tSkip ORF prediction (to save time)")
    parser.add_argument("--is_fusion", default=False, action="store_true", help="\t\tInput are fusion isoforms, must supply GTF as input using --gtf")
    parser.add_argument('-g', '--gtf', help='\t\tUse when running SQANTI by using as input a gtf of isoforms', action='store_true')
    parser.add_argument('-e','--expression', help='\t\tExpression matrix (supported: Kallisto tsv)', required=False)
    parser.add_argument('-x','--gmap_index', help='\t\tPath and prefix of the reference index created by gmap_build. Mandatory if using GMAP unless -g option is specified.')
    parser.add_argument('-t', '--gmap_threads', help='\t\tNumber of threads used during alignment by aligners.', required=False, default="1", type=int)
    #parser.add_argument('-z', '--sense', help='\t\tOption that helps aligners know that the exons in you cDNA sequences are in the correct sense. Applicable just when you have a high quality set of cDNA sequences', required=False, action='store_true')
    parser.add_argument('-o','--output', help='\t\tPrefix for output files.', required=False)
    parser.add_argument('-d','--dir', help='\t\tDirectory for output files. Default: Directory where the script was run.', required=False)
    parser.add_argument('-c','--coverage', help='\t\tJunction coverage files (provide a single file or a file pattern, ex: "mydir/*.junctions").', required=False)
    parser.add_argument('-s','--sites', default="ATAC,GCAG,GTAG", help='\t\tSet of splice sites to be considered as canonical (comma-separated list of splice sites). Default: GTAG,GCAG,ATAC.', required=False)
    parser.add_argument('-w','--window', default="20", help='\t\tSize of the window in the genomic DNA screened for Adenine content downstream of TTS', required=False, type=int)
    parser.add_argument('--geneid', help='\t\tUse gene_id tag from GTF to define genes. Default: gene_name used to define genes', default=False, action='store_true')
    parser.add_argument('-fl', '--fl_count', help='\t\tFull-length PacBio abundance file', required=False)
    parser.add_argument("-v", "--version", help="Display program version number.", action='version', version='SQANTI2 '+str(__version__))

    args = parser.parse_args()

    if args.is_fusion:
        print("WARNING: Currently if --is_fusion is used, no ORFs will be predicted.", file=sys.stderr)
        args.skipORF = True
        if not args.gtf:
            print("ERROR: if --is_fusion is on, must supply GTF as input and use --gtf!", file=sys.stderr)
            sys.exit(-1)

    if args.expression is not None:
        if not os.path.exists(args.expression):
            print("Expression file {0} not found. Abort!".format(args.expression), file=sys.stderr)
            sys.exit(-1)

    # path and prefix for output files
    if args.output is None:
        args.output = os.path.splitext(os.path.basename(args.isoforms))[0]

    if args.dir is None:
        args.dir = os.getcwd()
    else:
        if not os.path.isdir(os.path.abspath(args.dir)):
            print("ERROR: {0} directory doesn't exist. Abort!".format(args.dir), file=sys.stderr)
            sys.exit()
        else:
            args.dir = os.path.abspath(args.dir)

    args.genome = os.path.abspath(args.genome)
    if not os.path.isfile(args.genome):
        print("ERROR: genome fasta {0} doesn't exist. Abort!".format(args.genome), file=sys.stderr)
        sys.exit()

    args.isoforms = os.path.abspath(args.isoforms)
    if not os.path.isfile(args.isoforms):
        print("ERROR: Input isoforms {0} doesn't exist. Abort!".format(args.isoforms), file=sys.stderr)
        sys.exit()

    if not args.gtf:
        if args.aligner_choice == 'gmap':
            if not os.path.isdir(os.path.abspath(args.gmap_index)):
                print("GMAP index {0} doesn't exist! Abort.".format(args.gmap_index), file=sys.stderr)
                sys.exit()
        elif args.aligner_choice == 'deSALT':
            if not os.path.isdir(os.path.abspath(args.gmap_index)):
                print("deSALT index {0} doesn't exist! Abort.".format(args.gmap_index), file=sys.stderr)
                sys.exit()

        print("Cleaning up isoform IDs...", file=sys.stderr)
        args.isoforms = rename_isoform_seqids(args.isoforms, args.force_id_ignore)
        print("Cleaned up isoform fasta file written to: {0}".format(args.isoforms), file=sys.stderr)


    args.annotation = os.path.abspath(args.annotation)
    if not os.path.isfile(args.annotation):
        print("ERROR: Annotation doesn't exist. Abort!".format(args.annotation), file=sys.stderr)
        sys.exit()

    #if args.aligner_choice == "gmap":
    #    args.sense = "sense_force" if args.sense else "auto"
    #elif args.aligner_choice == "minimap2":
    #    args.sense = "f" if args.sense else "b"
    ## (Liz) turned off option for --sense, always TRUE
    if args.aligner_choice == "gmap":
        args.sense = "sense_force"
    elif args.aligner_choice == "minimap2":
        args.sense = "f"
    #elif args.aligner_choice == "deSALT":  #deSALT does not support this yet
    #    args.sense = "--trans-strand"

    # Running functionality
    print("**** Running SQANTI...", file=sys.stdout)
    run(args)


if __name__ == "__main__":
    main()
