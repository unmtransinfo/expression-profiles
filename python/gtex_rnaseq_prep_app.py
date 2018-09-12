#!/usr/bin/env python3
"""gtex_rnaseq_prep_app.py

GTEx RNAseq Preprocessing, for Sex As Biological Variable (SABV) analyses.

Command-line version; see also Jupyter notebook gtex_rnaseq_prep.ipynb

 - Author: Jeremy Yang
 - Based on R code by Oleg Ursu.
 - Required: Python3, Pandas 0.22+

Workflow (prep):
 - READ: GTEx Subjects data, 1-row/subject.
 - READ: GTEx Samples data, 1-row/sample.
 - READ: GTEx RNAseq expression TPM data, 1-row/gene, 1-col/sample.
 - READ: gene IDs file, from GTEx/Ensembl/HGNC, via gtex_gene_map.R. 
 - REMOVE: samples with Hardy score >2 (prefer healthier).
 - REMOVE: samples with high degree of autolysis (self-digestion).
 - MERGE: Samples and subjects, to 1-row/sample.
 - RESHAPE: RNAseq data from 1-col/sample, to 3 cols: gene, sample, TPM.
 - REMOVE: genes in pseudoautosomal regions (PAR) of chromosome Y.
 - AGGREGATE: samples, computing median TPM by gene+tissue.
 - AGGREGATE: samples, computing median TPM by gene+tissue+sex.
 - OUTPUT: median TPMs, 1-row/gene+tissue+sex.
 - OUTPUT: expression profiles, 1-row/gene+sex.

"""
#############################################################################
import sys,os,io,re,time,argparse
import pandas,numpy,scipy,scipy.stats

#############################################################################
### (GTEx_v7_Annotations_SubjectPhenotypesDS.txt)
#############################################################################
def ReadSubjects(ifile, verbose):
  fin = open(ifile)
  LOG('=== GTEx Subjects datafile: %s'%fin.name)
  subjects = pandas.read_csv(fin, sep='\t')
  LOG("Subjects dataset nrows: %d ; ncols: %d:"%(subjects.shape[0],subjects.shape[1]))
  return subjects

#############################################################################
### Format: one line per tissue name, in preferred order.
#############################################################################
def ReadTissues(ifile, verbose):
  fin = open(ifile)
  LOG('=== GTEx Tissues datafile: %s'%fin.name)
  tissues = pandas.read_csv(fin, sep=';', index_col=False, header=None, names=['name'])
  tissues = tissues.name.str.strip()
  if verbose: LOG("n_tissues: %d:"%(tissues.size))
  if verbose: LOG("tissues:\n%s"%(str(tissues)))
  return tissues

#############################################################################
### Keep only healthier subjects: 
### (DTHHRDY = 4-point Hardy Scale Death Classification.)
#############################################################################
def CleanSubjects(subjects, verbose):
  LOG("=== Subjects with Hardy score > 2 or NA: %d (removing)"%(subjects.query('DTHHRDY > 2').shape[0]))
  subjects = subjects.query('DTHHRDY <= 2')
  LOG("Subjects dataset nrows: %d ; ncols: %d:"%(subjects.shape[0],subjects.shape[1]))
  DescribeDf(subjects, verbose)
  return subjects

#############################################################################
def DescribeSubjects(subjects):
  LOG("=== DescribeSubjects:")
  for name,val in subjects.AGE.value_counts().sort_index().iteritems():
    LOG('\tAGE %s: %4d'%(name,val))
  for name,val in subjects.DTHHRDY.value_counts(sort=True, dropna=False).sort_index().iteritems():
    LOG('\tDTHHRDY %s: %4d'%(name,val))

#############################################################################
def DescribeDf(df, verbose):
  buff = io.StringIO()
  df.info(buf=buff,verbose=bool(verbose),null_counts=bool(verbose))
  LOG(re.sub(re.compile('^', re.M), '\t', buff.getvalue()))

#############################################################################
### (GTEx_v7_Annotations_SampleAttributesDS.txt)
#############################################################################
def ReadSamples(ifile, verbose):
  LOG("=== ReadSamples:")
  fin = open(ifile)
  LOG('GTEx Samples datafile: %s'%fin.name)
  samples = pandas.read_csv(fin, sep='\t')
  samples = samples[['SAMPID', 'SMATSSCR', 'SMTS', 'SMTSD']]
  LOG("Samples dataset nrows: %d ; ncols: %d:"%(samples.shape[0],samples.shape[1]))
  ### SUBJID is first two hyphen-delimted fields of SAMPID.
  samples['SUBJID'] = samples.SAMPID.str.extract('^([^-]+-[^-]+)-', expand=False)
  DescribeDf(samples, verbose)
  return samples

#############################################################################
### Clean & tidy cols. Remove samples with high degree of autolysis (self-digestion).
#############################################################################
def CleanSamples(samples, verbose):
  LOG("=== CleanSamples:")
  samples.dropna(how='any', inplace=True)
  samples.SEX = samples.SEX.apply(lambda x: 'F' if x==2 else 'M' if x==1 else None)
  samples = samples[samples.SMATSSCR < 2]
  samples.loc[(samples.SMTS.str.strip()=='') & samples.SMTSD.str.startswith("Skin -"), 'SMTS'] = 'Skin'
  LOG("Samples dataset nrows: %d ; ncols: %d:"%(samples.shape[0],samples.shape[1]))
  return samples

#############################################################################
def DescribeSamples(samples):
  LOG("=== DescribeSamples:")
  for name,val in samples.SEX.value_counts().sort_index().iteritems():
    LOG('\tSEX %s: %4d'%(name,val))
  i=0
  for name,val in samples.SMTSD.value_counts().sort_index().iteritems():
    i+=1
    LOG('\t%d. "%s": %4d'%(i,name,val))

#############################################################################
### READ GENE TPMs (full or demo subset)
### Top 2 rows, format:
###	#1.2
###	nrow	ncol
### Full file is ~56k rows, 2.6GB uncompressed.  Demo ~1k rows.
### *   GTEx_Analysis_2016-01-15_v7_RNASeQCv1.1.8_gene_tpm.gct.gz
### *   GTEx_Analysis_2016-01-15_v7_RNASeQCv1.1.8_gene_tpm_demo.gct.gz
#############################################################################
### Truncate ENSGV version, use un-versioned ENSG for mapping. Ok?
#############################################################################
def ReadRnaseq(ifile, verbose):
  LOG("=== ReadRnaseq:")
  fin = open(ifile, "rb")
  LOG('GTEx RNAseq TPM datafile: %s'%fin.name)
  rnaseq = pandas.read_table(fin, compression='gzip', sep='\t', skiprows=2)
  LOG("RNAseq dataset nrows: %d ; ncols: %d:"%(rnaseq.shape[0],rnaseq.shape[1]))
  rnaseq = rnaseq.drop(columns=['Description'])
  rnaseq = rnaseq.rename(columns={'Name':'ENSG'})
  rnaseq.ENSG = rnaseq.ENSG.str.extract('^([^\.]+)\..*$', expand=False)
  samples = rnaseq.columns[1:]
  LOG("RNAseq samples count: %d:"%(samples.size))
  LOG("RNAseq unique samples count: %d:"%(samples.nunique()))
  LOG("RNAseq genes (ENSG) count: %d:"%(rnaseq.ENSG.size))
  LOG("RNAseq unique genes (ENSG) count: %d:"%(rnaseq.ENSG.nunique()))
  return rnaseq

#############################################################################
### Read gene IDs, etc.: ENSG,NCBI,HGNCID,symbol,name
#############################################################################
def ReadGenes(ifile, verbose):
  LOG("=== ReadGenes:")
  fin = open(ifile)
  LOG('GTEx/Ensembl/HGNC genes datafile: %s'%fin.name)
  genes = pandas.read_csv(fin, sep='\t', na_values=[''], dtype={2:str})
  LOG("Genes dataset nrows: %d ; ncols: %d:"%(genes.shape[0],genes.shape[1]))
  #genes.columns = ['ENSG','NCBI','HGNC']
  #genes.dropna(inplace=True)
  return genes

#############################################################################
### Memory intensive. Divide task to manage memory use.
### For each tissue, group and concatenate results.
#############################################################################
def CleanRnaseq(rnaseq, verbose):
  if verbose: LOG("NOTE: CleanRnaseq IN: nrows = %d, cols: %s"%(rnaseq.shape[0],str(rnaseq.columns.tolist())))
  LOG("=== CleanRnaseq:")

  LOG("For each tissue, remove genes not expressed in both sexes...")
  for i,smtsd in enumerate(rnaseq.SMTSD.sort_values().unique()):
    rnaseq_this = rnaseq[rnaseq.SMTSD==smtsd]
    if rnaseq_this.SEX.nunique()<2: #Removes sex-specific tissues.
      LOG("\t%d. \"%s\" nsex_lt2 count (all): %d"%(i+1,smtsd,rnaseq_this.ENSG.nunique()))
      continue
    nsex_lt2 = (rnaseq_this[['ENSG','SEX']].groupby(by=['ENSG'], as_index=True).nunique()<2).rename(columns={'SEX':'nsex_lt2'})
    LOG("\t%d. \"%s\" nsex_lt2 count: %d"%(i+1,smtsd,nsex_lt2.nsex_lt2.value_counts()[True] if True in nsex_lt2.nsex_lt2.value_counts() else 0))
    rnaseq_this = pandas.merge(rnaseq_this, nsex_lt2, left_on=['ENSG'], right_index=True)
    rnaseq_this = rnaseq_this[~rnaseq_this['nsex_lt2']]
    rnaseq_this.drop(columns=['nsex_lt2'], inplace=True)
    if i==0:
      rnaseq_out=rnaseq_this
    else:
      rnaseq_out=pandas.concat([rnaseq_out,rnaseq_this])
  rnaseq = rnaseq_out

  ### Breast not 100% sex-specific, so manually remove.
  rnaseq = rnaseq[~rnaseq.SMTSD.str.match('^Breast')]

  LOG("For each tissue, remove genes with TPMs all zero...")
  for i,smtsd in enumerate(rnaseq.SMTSD.sort_values().unique()):
    rnaseq_this = rnaseq[rnaseq.SMTSD==smtsd]
    tpm_all0  = (rnaseq_this[['ENSG','TPM']].groupby(by=['ENSG'], as_index=True).max()==0).rename(columns={'TPM':'tpm_all0'})
    LOG("\t%d. \"%s\" tpm_all0 count: %d"%(i+1,smtsd,tpm_all0.tpm_all0.value_counts()[True] if True in tpm_all0.tpm_all0.value_counts() else 0))
    rnaseq_this = pandas.merge(rnaseq_this, tpm_all0, left_on=['ENSG'], right_index=True)
    rnaseq_this = rnaseq_this[~rnaseq_this['tpm_all0']]
    rnaseq_this.drop(columns=['tpm_all0'], inplace=True)
    if i==0:
      rnaseq_out=rnaseq_this
    else:
      rnaseq_out=pandas.concat([rnaseq_out,rnaseq_this])
  rnaseq = rnaseq_out

  rnaseq = rnaseq[['ENSG','SMTSD','SAMPID','SMATSSCR','SEX','AGE','DTHHRDY','TPM']]
  rnaseq = rnaseq.sort_values(by=['ENSG','SMTSD','SAMPID'])
  rnaseq = rnaseq.reset_index(drop=True)
  LOG("RNAseq unique samples count: %d:"%(rnaseq.SAMPID.nunique()))
  LOG("RNAseq unique tissues count: %d:"%(rnaseq.SMTSD.nunique()))
  LOG("RNAseq unique gene count: %d"%(rnaseq.ENSG.nunique()))
  if verbose: LOG("NOTE: CleanRnaseq OUT: nrows = %d, cols: %s"%(rnaseq.shape[0],str(rnaseq.columns.tolist())))
  return rnaseq

#############################################################################
### Compute median TPM by gene+tissue+sex.
#############################################################################
def SABV_aggregate_median(rnaseq, verbose):
  LOG("=== SABV_aggregate_median:")
  if verbose: LOG("NOTE: SABV_aggregate_median IN: nrows = %d, cols: %s"%(rnaseq.shape[0],str(rnaseq.columns.tolist())))
  rnaseq = rnaseq[['ENSG', 'SMTSD', 'SEX', 'TPM']].groupby(by=['ENSG','SMTSD','SEX'], as_index=False).median()
  if verbose: LOG("NOTE: SABV_aggregate_median OUT: nrows = %d, cols: %s"%(rnaseq.shape[0],str(rnaseq.columns.tolist())))
  return rnaseq

#############################################################################
### Reshape to one-row-per-gene format.
### From:   ENSG,SMTSD,SEX,TPM,LOG_TPM
### To:	    ENSG,SEX,TPM_1,TPM_2,...TPM_N (N tissues)
### Preserve tissue order.
#############################################################################
def PivotToProfiles(rnaseq, tissues_ordered, verbose):
  if verbose: LOG("NOTE: PivotToProfiles IN: nrows = %d, cols: %s"%(rnaseq.shape[0],str(rnaseq.columns.tolist())))
  tissues = pandas.Series(pandas.unique(rnaseq.SMTSD.sort_values()))
  if type(tissues_ordered)==pandas.core.series.Series:
    if set(tissues) == set(tissues_ordered):
      tissues = tissues_ordered
      LOG("Note: input tissues (ordered): %s"%(str(set(tissues))))
    else:
      LOG("Warning: input tissues missing in samples: %s"%(str(set(tissues_ordered) - set(tissues))))
      LOG("Warning: sample tissues missing in input: %s"%(str(set(tissues) - set(tissues_ordered))))

  # Assure only 1-row per unique (ensg,smtsd) tuple (or pivot will fail).
  #rnaseq = rnaseq.drop_duplicates(subset=['ENSG','SMTSD'], keep='first')

  rnaseq_f = rnaseq[rnaseq.SEX=='F'].drop(columns=['SEX'])
  rnaseq_m = rnaseq[rnaseq.SEX=='M'].drop(columns=['SEX'])

  rnaseq_f = rnaseq_f[['ENSG','SMTSD','TPM']]
  rnaseq_m = rnaseq_m[['ENSG','SMTSD','TPM']]

  exfiles_f = rnaseq_f.pivot(index='ENSG', columns='SMTSD')
  exfiles_f.columns = exfiles_f.columns.get_level_values(1)
  exfiles_f = exfiles_f.reset_index(drop=False)
  exfiles_f['SEX'] = 'F'
  exfiles_m = rnaseq_m.pivot(index='ENSG', columns='SMTSD')
  exfiles_m.columns = exfiles_m.columns.get_level_values(1)
  exfiles_m = exfiles_m.reset_index(drop=False)
  exfiles_m['SEX'] = 'M'
  exfiles = pandas.concat([exfiles_f,exfiles_m])
  cols = ['ENSG','SEX']+tissues.tolist()
  exfiles = exfiles[cols]
  DescribeDf(exfiles,verbose)
  if verbose: LOG("NOTE: PivotToProfiles OUT: nrows = %d, cols: %s"%(exfiles.shape[0],str(exfiles.columns.tolist())))
  return exfiles

#############################################################################
def LOG(msg, file=sys.stdout, flush=True):
  print(msg, file=file, flush=True)

#############################################################################
if __name__=='__main__':
  parser = argparse.ArgumentParser(description='GTEx RNAseq Exfiles/SABV preprocessor')
  parser.add_argument("--i_subject",dest="ifile_subject",help="input subjects file")
  parser.add_argument("--i_sample",dest="ifile_sample",help="input samples file")
  parser.add_argument("--i_rnaseq",dest="ifile_rnaseq",help="input rnaseq file")
  parser.add_argument("--i_gene",dest="ifile_gene",help="input gene file")
  parser.add_argument("--i_tissue",dest="ifile_tissue",help="input (ordered) tissue file")
  parser.add_argument("--o_median",dest="ofile_median",help="output median TPM, 1-row/gene+tissue+sex (TSV)")
  parser.add_argument("--o_sample",dest="ofile_sample",help="output sample TPM, 1-row/gene+sample (TSV)")
  parser.add_argument("--o_profiles",dest="ofile_profiles",help="output profiles, 1-row/gene+sex (TSV)")
  parser.add_argument("--o_tissue",dest="ofile_tissue",help="output tissues (TSV)")
  parser.add_argument("--decimals",type=int,default=3,help="output decimal places")
  parser.add_argument("-v","--verbose",action="count")
  args = parser.parse_args()

  PROG=os.path.basename(sys.argv[0])
  t0 = time.time()

  if args.verbose:
    LOG('Python: %s; Pandas: %s; Scipy: %s ; Numpy: %s'%(sys.version.split()[0],pandas.__version__,scipy.__version__,numpy.__version__))

  if args.ifile_tissue:
    tissues = ReadTissues(args.ifile_tissue, args.verbose)
  else:
    tissues = None

  if not args.ifile_subject:
    parser.error('Input subject file required.')

  subjects = ReadSubjects(args.ifile_subject, args.verbose)

  if args.verbose:
    DescribeSubjects(subjects)

  subjects = CleanSubjects(subjects, args.verbose)

  if not args.ifile_sample:
    parser.error('Input sample file required.')
  samples = ReadSamples(args.ifile_sample, args.verbose)

  LOG('=== MERGE samples and subjects:')
  samples = pandas.merge(samples, subjects, how='inner', on='SUBJID')

  if args.verbose:
    DescribeSamples(samples)

  if args.ofile_tissue:
    sample_tissues = samples[['SMTS','SMTSD']].reset_index(drop=True)
    sample_tissues = sample_tissues.drop_duplicates().sort_values(['SMTS', 'SMTSD'])
    LOG("=== Output tissues file: %s"%args.ofile_tissue)
    sample_tissues.round(args.decimals).to_csv(args.ofile_tissue, sep='\t', index=False)

  samples = CleanSamples(samples, args.verbose)

  if not args.ifile_gene:
    parser.error('Input gene file required.')
  genes = ReadGenes(args.ifile_gene, args.verbose)

  if not args.ifile_rnaseq:
    parser.error('Input RNAseq file required.')
  t1 = time.time()
  rnaseq = ReadRnaseq(args.ifile_rnaseq, args.verbose)
  LOG("ReadRnaseq elapsed: %ds"%(time.time()-t1))

  # Merge/inner with gene IDs file, to retain only protein-coding genes.
  rnaseq = pandas.merge(rnaseq, genes[['ENSG']], on='ENSG', how='inner')
  LOG("RNAseq unique gene count (inner join with protein-coding gene ENSGs): %d"%(rnaseq.ENSG.nunique()))

  LOG('=== Remove genes in pseudoautosomal regions (PAR) of chromosome Y ("ENSGR"):')
  n_ensgr = rnaseq.ENSG.str.startswith('ENSGR').sum()
  LOG('ENSGR gene TPMs: %d (%.2f%%)'%(n_ensgr,100*n_ensgr/rnaseq.shape[0]))
  rnaseq = rnaseq[~rnaseq.ENSG.str.startswith('ENSGR')]
  LOG("RNAseq unique gene count (after PAR removal): %d"%(rnaseq.ENSG.nunique()))

  LOG('=== MELT: One row per ENSG+SAMPID+TPM triplet:')
  ### Easier to handle but ~3x storage.
  rnaseq = rnaseq.melt(id_vars = "ENSG", var_name = "SAMPID", value_name = "TPM")
  DescribeDf(rnaseq,args.verbose)
  LOG("RNAseq unique gene count (after melt): %d"%(rnaseq.ENSG.nunique()))

  # Merge/inner with gene IDs file. This time to add IDs, names.
  rnaseq = pandas.merge(rnaseq, genes, on='ENSG', how='left')
  LOG("RNAseq unique gene count (after merge with gene IDs): %d"%(rnaseq.ENSG.nunique()))

  LOG('=== Merge with samples:')
  rnaseq = pandas.merge(rnaseq, samples, how="inner", on="SAMPID")
  LOG("RNAseq unique gene count (after merge with samples): %d"%(rnaseq.ENSG.nunique()))

  rnaseq = CleanRnaseq(rnaseq, args.verbose)

  if args.ofile_sample:
    LOG("=== Output sample TPM file: %s"%args.ofile_sample)
    rnaseq.round(args.decimals).to_csv(args.ofile_sample, sep='\t', index=False)

  LOG('=== Compute median TPM by gene+tissue+sex:')
  rnaseq = SABV_aggregate_median(rnaseq, args.verbose)

  LOG("SABV TPM median unique counts: genes: %d"%(rnaseq.ENSG.nunique()))

  if args.ofile_median:
    LOG("=== Output median (by gene+tissue+sex) TPM file: %s"%args.ofile_median)
    rnaseq.round(args.decimals).to_csv(args.ofile_median, sep='\t', index=False)

  LOG("=== Pivot to one-row-per-gene format (profiles).")
  rnaseq_profiles = PivotToProfiles(rnaseq, tissues, args.verbose)
  if args.ofile_profiles:
    LOG("=== Output profiles file: %s"%args.ofile_profiles)
    rnaseq_profiles.round(args.decimals).to_csv(args.ofile_profiles, sep='\t', index=False)

  LOG("%s Elapsed: %ds"%(PROG,(time.time()-t0)))
