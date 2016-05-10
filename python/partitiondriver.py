import time
import sys
import itertools
import math
import os
import glob
import csv
csv.field_size_limit(sys.maxsize)  # make sure we can write very large csv fields
import random
from collections import OrderedDict
from subprocess import Popen, check_call, PIPE, CalledProcessError
import copy
import multiprocessing

import utils
from opener import opener
from seqfileopener import get_seqfile_info
import annotationclustering
from glomerator import Glomerator
from clusterpath import ClusterPath
from waterer import Waterer
from parametercounter import ParameterCounter
from performanceplotter import PerformancePlotter
from hist import Hist

# ----------------------------------------------------------------------------------------
class PartitionDriver(object):
    """ Class to parse input files, start bcrham jobs, and parse/interpret bcrham output for annotation and partitioning """
    def __init__(self, args):
        self.args = args
        self.glfo = utils.read_germline_set(self.args.datadir, debug=True)

        self.input_info, self.reco_info = get_seqfile_info(self.args.seqfile, self.args.is_data, self.glfo, self.args.n_max_queries, self.args.queries, self.args.reco_ids,
                                                           name_column=self.args.name_column, seq_column=self.args.seq_column, seed_unique_id=self.args.seed_unique_id,
                                                           abbreviate_names=self.args.abbreviate)
        self.sw_info = None
        self.bcrham_proc_info = None
        self.bcrham_failed_queries = set()

        self.n_max_calc_per_process = 200  # if a bcrham process calc'd more than this many fwd + vtb values, don't decrease the number of processes in the next step

        self.unseeded_clusters = set()  # all the queries that we *didn't* cluster with the seed uid
        self.time_to_remove_unseeded_clusters = False
        self.already_removed_unseeded_clusters = False

        self.hmm_infname = self.args.workdir + '/hmm_input.csv'
        self.hmm_cachefname = self.args.workdir + '/hmm_cached_info.csv'
        self.hmm_outfname = self.args.workdir + '/hmm_output.csv'
        self.annotation_fname = self.hmm_outfname.replace('.csv', '_annotations.csv')  # TODO won't work in parallel

        utils.prep_dir(self.args.workdir)
        if self.args.outfname is not None:
            outdir = os.path.dirname(self.args.outfname)
            if outdir != '' and not os.path.exists(outdir):
                os.makedirs(outdir)

        if self.args.persistent_cachefname is not None:
            if os.path.exists(self.args.persistent_cachefname):  # if it exists, copy it to workdir
                check_call(['cp', '-v', self.args.persistent_cachefname, self.hmm_cachefname])
            else:  # otherwise create it with just headers
                pass  # hm, maybe do it in ham

        if len(self.input_info) > 1000 and self.args.n_procs == 1:
            print '\nhey there! I see you\'ve got %d sequences spread over %d processes. This will be kinda slow, so it might be a good idea to increase --n-procs (see the manual for suggestions on how many for annotation and partitioning).\n' % (len(self.input_info), self.args.n_procs)
        if len(self.input_info) > 10000 and self.args.action != 'cache-parameters' and self.args.outfname is None:
            print '\nwarning: running on a lot of sequences without setting --outfname. Which is ok! But there\'ll be no persistent record of the results'

    # ----------------------------------------------------------------------------------------
    def clean(self):
        # merge persistent and current cache files into the persistent cache file
        if self.args.persistent_cachefname is not None:
            lockfname = self.args.persistent_cachefname + '.lock'
            while os.path.exists(lockfname):
                print '  waiting for lock on %s' % lockfname
                time.sleep(0.5)
            lockfile = open(lockfname, 'w')
            if not os.path.exists(self.args.persistent_cachefname):
                open(self.args.persistent_cachefname, 'w').close()
            self.merge_files(infnames=[self.args.persistent_cachefname, self.hmm_cachefname], outfname=self.args.persistent_cachefname, dereplicate=True)
            lockfile.close()
            os.remove(lockfname)
        if not self.args.no_clean and os.path.exists(self.hmm_cachefname):
            os.remove(self.hmm_cachefname)

        if not self.args.no_clean:
            try:
                os.rmdir(self.args.workdir)
            except OSError:
                raise Exception('workdir (%s) not empty: %s' % (self.args.workdir, ' '.join(os.listdir(self.args.workdir))))  # hm... you get weird recursive exceptions if you get here. Oh, well, it still works

    # ----------------------------------------------------------------------------------------
    def run_waterer(self, parameter_dir, write_parameters=False):
        start = time.time()
        if write_parameters:  # if we're writing parameters, then we don't have any hmm dir to look in
            genes_to_use = self.args.only_genes  # if None, we use all of 'em
        else:  # ...but if we're not writing parameters, then we want to look in the existing parameter dir to see for which genes we have hmms, and then tell sw to only use those
            genes_to_use = utils.find_genes_that_have_hmms(parameter_dir)
            if self.args.only_genes is not None:
                genes_to_use = list(set(genes_to_use) & set(self.args.only_genes))  # we have to have an hmm for it, and it has to be among the genes that were specified on the command line

        waterer = Waterer(self.args, self.input_info, self.reco_info, self.glfo, parameter_dir, write_parameters, genes_to_use)
        waterer.run()
        self.sw_info = waterer.info
        print '        water time: %.1f' % (time.time()-start)

    # ----------------------------------------------------------------------------------------
    def cache_parameters(self):
        """ Infer full parameter sets and write hmm files for sequences from <self.input_info>, first with Smith-Waterman, then using the SW output as seed for the HMM """
        sw_parameter_dir = self.args.parameter_dir + '/sw'
        self.run_waterer(sw_parameter_dir, write_parameters=True)
        self.write_hmms(sw_parameter_dir)
        parameter_out_dir = self.args.parameter_dir + '/hmm'
        self.run_hmm('viterbi', parameter_in_dir=sw_parameter_dir, parameter_out_dir=parameter_out_dir, count_parameters=True)
        self.write_hmms(parameter_out_dir)

    # ----------------------------------------------------------------------------------------
    def run_algorithm(self, algorithm):
        """ Just run <algorithm> (either 'forward' or 'viterbi') on sequences in <self.input_info> and exit. You've got to already have parameters cached in <self.args.parameter_dir> """
        self.run_waterer(self.args.parameter_dir)
        self.run_hmm(algorithm, parameter_in_dir=self.args.parameter_dir)

    # ----------------------------------------------------------------------------------------
    def partition(self):
        """ Partition sequences in <self.input_info> into clonally related lineages """
        self.run_waterer(self.args.parameter_dir)  # run smith-waterman

        # cache hmm naive seq for each single query
        if len(self.sw_info['queries']) > 50 or self.args.naive_vsearch or self.args.naive_swarm:
            self.run_hmm('viterbi', self.args.parameter_dir, n_procs=self.get_n_precache_procs(), precache_all_naive_seqs=True)

        if self.args.naive_vsearch or self.args.naive_swarm:
            self.cluster_with_naive_vsearch_or_swarm(self.args.parameter_dir)
            return

        n_procs = self.args.n_procs
        cpath = ClusterPath(seed_unique_id=self.args.seed_unique_id)
        cpath.add_partition([[cl, ] for cl in self.sw_info['queries']], logprob=0., n_procs=n_procs)  # NOTE sw info excludes failed sequences
        n_proc_list = []
        start = time.time()
        while n_procs > 0:
            print '--> %d clusters with %d procs' % (len(cpath.partitions[cpath.i_best_minus_x]), n_procs)  # write_hmm_input uses the best-minus-ten partition
            cpath = self.run_hmm('forward', self.args.parameter_dir, n_procs=n_procs, cpath=cpath)
            n_proc_list.append(n_procs)
            if n_procs == 1:
                break
            n_procs = self.get_next_n_procs(n_procs, n_proc_list, cpath)

        print '      loop time: %.1f' % (time.time()-start)

        if self.args.debug:
            print 'final'
            cpath.print_partitions(self.reco_info, print_header=True, calc_missing_values='all' if (len(self.input_info) < 500) else 'best')
            if not self.args.is_data:
                true_cp = ClusterPath(seed_unique_id=self.args.seed_unique_id)
                true_cp.add_partition(utils.get_true_partition(self.reco_info), -1., 1)
                print 'true:'
                true_cp.print_partitions(self.reco_info, print_header=False, calc_missing_values='best')

        self.check_partition(cpath.partitions[cpath.i_best])
        if self.args.print_cluster_annotations:
            self.read_annotation_output(self.annotation_fname)
        if self.args.outfname is not None:
            self.write_clusterpaths(self.args.outfname, cpath)  # [last agglomeration step]

    # ----------------------------------------------------------------------------------------
    def get_next_n_procs(self, n_procs, n_proc_list, cpath):
        next_n_procs = n_procs

        n_calcd_per_process = self.get_n_calculated_per_process()
        factor = 1.3

        reduce_n_procs = False
        if n_calcd_per_process < self.n_max_calc_per_process or n_proc_list.count(n_procs) > n_procs:  # if we didn't need to do that many calculations, or if we've already milked this number of procs for most of what it's worth
            reduce_n_procs = True

        if reduce_n_procs:
            next_n_procs = int(next_n_procs / float(factor))

        # time to remove unseeded clusters?
        if self.args.seed_unique_id is not None and (len(n_proc_list) > 2 or next_n_procs == 1):
            if not self.already_removed_unseeded_clusters:  # if we didn't already remove the unseeded clusters in a previous step
                print '     time to remove unseeded clusters'
                self.time_to_remove_unseeded_clusters = True  # (they don't actually get removed until we're writing hmm input)
                initial_seqs_per_proc = int(float(len(self.input_info)) / n_proc_list[0])
                self.unseeded_clusters = self.get_unseeded_clusters(cpath.partitions[cpath.i_best_minus_x])
                n_remaining_seqs = len(self.input_info) - len(self.unseeded_clusters)
                integer = 3  # multiply by something 'cause we're turning off the seed uid for the last few times through
                next_n_procs = max(1, integer * int(float(n_remaining_seqs) / initial_seqs_per_proc))
                print '        new n_procs %d = %d * %d / %d' % (next_n_procs, integer, n_remaining_seqs, initial_seqs_per_proc)
            else:
                self.time_to_remove_unseeded_clusters = False  # will already be false after the first time
                print '     already removed unseeded clusters, proceed with n procs %d' % next_n_procs

        return next_n_procs

    # ----------------------------------------------------------------------------------------
    def get_n_calculated_per_process(self):
        if self.bcrham_proc_info is None:
            return

        total = 0.  # sum over each process
        for procinfo in self.bcrham_proc_info:
            if 'vtb' not in procinfo['calcd'] or 'fwd' not in procinfo['calcd']:
                print 'WARNING couldn\'t find vtb/fwd in:\n%s' % procinfo['calcd']  # may as well not fail, it probably just means we lost some stdout somewhere. Which, ok, is bad, but let's say it shouldn't be fatal.
                return 1.  # er, or something?
            if self.args.naive_hamming:
                assert procinfo['calcd']['fwd'] == 0.
            total += procinfo['calcd']['vtb'] + procinfo['calcd']['fwd']
        print '          n calcd: %d (%.1f per proc)' % (total, float(total) / len(self.bcrham_proc_info))
        return float(total) / len(self.bcrham_proc_info)

    # ----------------------------------------------------------------------------------------
    def check_partition(self, partition):
        uids = set([uid for cluster in partition for uid in cluster])
        input_ids = set(self.input_info.keys())  # maybe should switch this to self.sw_info['queries']? at least if we want to not worry about missing failed sw queries
        missing_ids = input_ids - uids - self.unseeded_clusters
        if len(missing_ids) > 0:
            warnstr = 'queries missing from partition: ' + ' '.join(missing_ids)
            print '  ' + utils.color('red', 'warning') + ' ' + warnstr

    # ----------------------------------------------------------------------------------------
    def get_n_precache_procs(self):
        if self.args.n_precache_procs is not None:
            return self.args.n_precache_procs

        n_seqs = len(self.sw_info['queries'])
        seqs_per_proc = 500  # 2.5 mins (at something like 0.3 sec/seq)
        if n_seqs > 3000:
            seqs_per_proc *= 2
        if n_seqs > 10000:
            seqs_per_proc *= 1.5
        n_precache_procs = int(math.ceil(float(n_seqs) / seqs_per_proc))
        n_precache_procs = min(n_precache_procs, self.args.n_max_procs)  # I can't get more'n a few hundred slots at a time, so it isn't worth using too much more than that
        if not self.args.slurm and not utils.auto_slurm(self.args.n_procs):  # if we're not on slurm, make sure it's less than the number of cpus
            n_precache_procs = min(n_precache_procs, multiprocessing.cpu_count())

        return n_precache_procs

    # ----------------------------------------------------------------------------------------
    def write_clusterpaths(self, outfname, cpath):
        outfile, writer = cpath.init_outfile(outfname, self.args.is_data)
        true_partition = None
        if not self.args.is_data:
            true_partition = utils.get_true_partition(self.reco_info)

        cpath.write_partitions(writer=writer, reco_info=self.reco_info, true_partition=true_partition, is_data=self.args.is_data, n_to_write=self.args.n_partitions_to_write, calc_missing_values='best')

        outfile.close()

    # ----------------------------------------------------------------------------------------
    def cluster_with_naive_vsearch_or_swarm(self, parameter_dir):
        start = time.time()
        # read cached naive seqs
        naive_seqs = {}
        with open(self.hmm_cachefname) as cachefile:
            reader = csv.DictReader(cachefile)
            for line in reader:
                unique_ids = line['unique_ids'].split(':')
                assert len(unique_ids) == 1
                unique_id = unique_ids[0]
                naive_seqs[unique_id] = line['naive_seq']

        # make a fasta file
        fastafname = self.args.workdir + '/simu.fasta'
                
        # if not os.path.exists(fastafname):
        if self.args.naive_swarm:
            print '    NOTE: replacing N with A for input to swarm'
        with open(fastafname, 'w') as fastafile:
            for query, naive_seq in naive_seqs.items():
                if self.args.naive_swarm:
                    query += '_1'
                    naive_seq = utils.remove_ambiguous_ends(naive_seq)
                    naive_seq = naive_seq.replace('N', 'A')
                fastafile.write('>' + query + '\n' + naive_seq + '\n')

        if self.args.naive_vsearch:
            # bound = self.get_naive_hamming_threshold(parameter_dir, 'tight') /  2.  # yay for heuristics! (I did actually optimize this...)
            # hfrac_bounds = self.get_naive_hamming_bounds(parameter_dir)
            # bound = hfrac_bounds[0] / 2.  # lo and hi are the same
            bound = self.get_naive_hamming_bounds(parameter_dir)[0]  # lo and hi are the same
            print '    using hfrac bound for vsearch %.3f' % bound
            id_fraction = 1. - bound
            clusterfname = self.args.workdir + '/vsearch-clusters.txt'
            cmd = './bin/vsearch-1.1.3-linux-x86_64 --threads ' + str(self.args.n_procs) + ' --uc ' + clusterfname + ' --cluster_fast ' + fastafname + ' --id ' + str(id_fraction) + ' --maxaccept 0 --maxreject 0'
            if self.args.slurm or utils.auto_slurm(self.args.n_procs):
                clust_err_file = self.args.error_prefix + "partition_vsearch.err"
                clust_out_file = self.args.error_prefix + "partition_vsearch.out"
                clust_string = "-e " + clust_err_file + " -o " + clust_out_file
                cmd = 'qsub -sync y -b y -V ' + clust_string + ' ' + cmd
            proc = Popen(cmd.split(), stdout=PIPE, stderr=PIPE)
            out, err = proc.communicate()
            exit_code = proc.wait()
            joinstr = '\n    '
            if out != '':
                print '  out:'
                print '    ' + joinstr.join(out.replace('\r', '').split('\n'))
            if err != '':
                print '  err:'
                print '    ' + joinstr.join(err.replace('\r', '').split('\n'))
            if exit_code != 0:
                raise Exception('vsearch failed with exit code %d' % exit_code)
    
        elif self.args.naive_swarm:
            clusterfname = self.args.workdir + '/swarm-clusters.txt'
            cmd = './bin/swarm-2.1.1-linux-x86_64 ' + fastafname
            cmd += ' -t 5'  # five threads TODO set this more intelligently
            # cmd += ' -f'
            cmd += ' --match-reward ' + str(self.args.match_mismatch[0])
            cmd += ' --mismatch-penalty ' + str(self.args.match_mismatch[1])
            cmd += ' --gap-opening-penalty ' + str(self.args.gap_open_penalty)
            # cmd += ' --gap-extension-penalty'
            tmpstart = time.time()
            total = 0.
            for key in self.sw_info['queries']:
                # padded sequence is here: self.sw_info[key]['padded']['seq']
                # but this should be un-padded
                seq = self.input_info[key]['seq']  # TODO hm, should this be from sw_info?
                total += float(len(seq))
            mean_length = total / len(self.input_info)  # TODO hm, should this be from sw_info?
            raise Exception('update for new thresholds')
            bound = self.get_naive_hamming_threshold(parameter_dir, 'tight') /  2.  # yay for heuristics! (I did actually optimize this...)
            differences = int(round(mean_length * bound))
            print '        d = mean len * mut freq bound = %f * %f = %f --> %d' % (mean_length, bound, mean_length * bound, differences)
            print '      swarm average time: %.1f' % (time.time()-tmpstart)
            cmd += ' --differences ' + str(differences)
            cmd += ' --uclust-file ' + clusterfname
            check_call(cmd.split())
        else:
            assert False

        # read output
        id_clusters = {}
        with open(clusterfname) as clusterfile:
            reader = csv.DictReader(clusterfile, fieldnames=['type', 'cluster_id', '3', '4', '5', '6', '7', 'crap', 'query', 'morecrap'], delimiter='\t')
            for line in reader:
                if line['type'] == 'C':  # batshit output format: some lines are a cluster, and some are a query sequence. Skip the cluster ones.
                    continue
                cluster_id = int(line['cluster_id'])
                if cluster_id not in id_clusters:
                    id_clusters[cluster_id] = []
                uid = line['query']
                if self.args.naive_swarm and uid[-2:] == '_1':  # remove (dummy) abundance information
                    uid = uid[:-2]
                id_clusters[cluster_id].append(uid)
        partition = id_clusters.values()
        self.check_partition(partition)
        ccfs = [None, None]
        if not self.args.is_data:  # it's ok to always calculate this since it's only ever for one partition
            true_partition = utils.get_true_partition(self.reco_info)
            ccfs = utils.new_ccfs_that_need_better_names(partition, true_partition, self.reco_info)
        cpath = ClusterPath(seed_unique_id=self.args.seed_unique_id)
        cpath.add_partition(partition, logprob=0.0, n_procs=1, ccfs=ccfs)
        if self.args.outfname is not None:
            self.write_clusterpaths(self.args.outfname, cpath)

        if not self.args.no_clean:
            os.remove(fastafname)
            os.remove(clusterfname)

        print '      vsearch/swarm time: %.1f' % (time.time()-start)

    # ----------------------------------------------------------------------------------------
    def get_naive_hamming_bounds(self, parameter_dir):
        if self.args.naive_hamming_bounds is not None:  # let the command line override auto bound calculation
            print '       naive hfrac bounds: %.3f %.3f' % tuple(self.args.naive_hamming_bounds)
            return self.args.naive_hamming_bounds

        mutehist = Hist(fname=parameter_dir + '/all-mean-mute-freqs.csv')
        mute_freq = mutehist.get_mean(ignore_overflows=True)

        # just use a line based on two points (mute_freq, threshold)
        x1, x2 = 0.05, 0.2  # 0.5x, 3x (for 10 leaves)

        if self.args.naive_hamming:  # set lo and hi to the same thing, so we don't use log prob ratios, i.e. merge if less than this, don't merge if greater than this
            y1, y2 = 0.035, 0.06
            lo = utils.intexterpolate(x1, y1, x2, y2, mute_freq)
            hi = lo
        elif self.args.naive_vsearch:  # set lo and hi to the same thing, so we don't use log prob ratios, i.e. merge if less than this, don't merge if greater than this
            y1, y2 = 0.02, 0.05
            lo = utils.intexterpolate(x1, y1, x2, y2, mute_freq)
            hi = lo
        else:  # these are a bit larger than the tight ones and should almost never merge non-clonal sequences, i.e. they're appropriate for naive hamming preclustering if you're going to run the full likelihood on nearby sequences
            y1, y2 = 0.015, 0.015  # TODO get better numbers for this
            lo = utils.intexterpolate(x1, y1, x2, y2, mute_freq)  # ...and never merge 'em if it's bigger than this
            y1, y2 = 0.08, 0.15
            hi = utils.intexterpolate(x1, y1, x2, y2, mute_freq)  # ...and never merge 'em if it's bigger than this

        print '       naive hfrac bounds: %.3f %.3f   (%.3f mutation in %s)' % (lo, hi, mute_freq, parameter_dir)
        return [lo, hi]

    # ----------------------------------------------------------------------------------------
    def get_hmm_cmd_str(self, algorithm, csv_infname, csv_outfname, parameter_dir, precache_all_naive_seqs, n_procs):
        """ Return the appropriate bcrham command string """
        cmd_str = os.getenv('PWD') + '/packages/ham/bcrham'
        if self.args.slurm or utils.auto_slurm(n_procs):
            clust_err_file = self.args.error_prefix + "_partition_hmm.err"
            clust_out_file = self.args.error_prefix + "_partition_hmm.out"
            clust_string = "-e " + clust_err_file + " -o " + clust_out_file
            cmd_str = 'qsub -sync y -b y -V ' + clust_string + ' -l h_vmem=24G,mem_token=24G,mem_free=24G ' + cmd_str
        cmd_str += ' --algorithm ' + algorithm
        if self.args.n_best_events is not None:
            cmd_str += ' --n_best_events ' + str(int(self.args.n_best_events))
        if self.args.debug > 0:
            cmd_str += ' --debug ' + str(self.args.debug)
        cmd_str += ' --hmmdir ' + os.path.abspath(parameter_dir) + '/hmms'
        cmd_str += ' --datadir ' + self.args.datadir  # NOTE waterer is using a rewritten datadir in the workdir if <only_genes> is specified... maybe I should switch this as well?
        cmd_str += ' --infile ' + csv_infname
        cmd_str += ' --outfile ' + csv_outfname
        cmd_str += ' --random-seed ' + str(self.args.seed)
        cmd_str += ' --biggest-naive-seq-cluster-to-calculate ' + str(self.args.biggest_naive_seq_cluster_to_calculate)
        cmd_str += ' --biggest-logprob-cluster-to-calculate ' + str(self.args.biggest_logprob_cluster_to_calculate)
        if self.args.cache_naive_hfracs:
            cmd_str += ' --cache-naive-hfracs'
        if n_procs > 1:  # only cache vals for sequence sets with newly-calculated vals (initial cache file is copied to each subdir)
            cmd_str += ' --only-cache-new-vals'

        if self.args.dont_rescale_emissions:
            cmd_str += ' --dont-rescale-emissions'
        if self.args.print_cluster_annotations and n_procs == 1:
            cmd_str += ' --annotationfile ' + self.annotation_fname
        if self.args.action == 'partition':
            cmd_str += ' --cachefile ' + self.hmm_cachefname
            if precache_all_naive_seqs:
                cmd_str += ' --cache-naive-seqs'
            else:  # actually partitioning
                cmd_str += ' --partition'
                cmd_str += ' --max-logprob-drop ' + str(self.args.max_logprob_drop)

                hfrac_bounds = self.get_naive_hamming_bounds(parameter_dir)
                if self.args.naive_hamming:  # shouldn't be able to happen, but...
                    assert hfrac_bounds[0] == hfrac_bounds[1]
                cmd_str += ' --hamming-fraction-bound-lo ' + str(hfrac_bounds[0])
                cmd_str += ' --hamming-fraction-bound-hi ' + str(hfrac_bounds[1])
                cmd_str += ' --logprob-ratio-threshold ' + str(self.args.logprob_ratio_threshold)
                if self.args.seed_unique_id is not None and not (self.already_removed_unseeded_clusters or self.time_to_remove_unseeded_clusters):  # if we're in the last few cycles (i.e. we've removed unseeded clusters) we want bcrham to not know about the seed (this gives more accurate clustering 'cause we're really doing hierarchical agglomeration)
                    cmd_str += ' --seed-unique-id ' + self.args.seed_unique_id

        assert len(utils.ambiguous_bases) == 1  # could allow more than one, but it's not implemented a.t.m.
        cmd_str += ' --ambig-base ' + utils.ambiguous_bases[0]

        return cmd_str

    # ----------------------------------------------------------------------------------------
    def subworkdir(self, iproc, n_procs):
        if n_procs == 1:
            return self.args.workdir
        else:
            return self.args.workdir + '/hmm-' + str(iproc)

    # ----------------------------------------------------------------------------------------
    def check_wait_times(self, wait_time):
        max_bcrham_time = max([procinfo['time']['bcrham'] for procinfo in self.bcrham_proc_info])
        if max_bcrham_time > 0. and wait_time / max_bcrham_time > 1.5 and wait_time > 30.:  # if we were waiting for a lot longer than the slowest process took, and if it took long enough for us to care
            print '    spent much longer waiting for bcrham (%.1fs) than bcrham reported taking (%.1fs)' % (wait_time, max_bcrham_time)

    # ----------------------------------------------------------------------------------------
    def execute(self, cmd_str, n_procs):
        # ----------------------------------------------------------------------------------------
        def get_outfname(iproc):
            return self.hmm_outfname.replace(self.args.workdir, self.subworkdir(iproc, n_procs))
        # ----------------------------------------------------------------------------------------
        def get_cmd_str(iproc):
            return cmd_str.replace(self.args.workdir, self.subworkdir(iproc, n_procs))

        print '    running %d procs' % n_procs
        sys.stdout.flush()
        start = time.time()

        # start all the procs for the first time
        procs, n_tries, = [], []
        self.bcrham_proc_info = []
        for iproc in range(n_procs):
            # print get_cmd_str(iproc)
            # sys.exit()
            procs.append(utils.run_cmd(get_cmd_str(iproc), self.subworkdir(iproc, n_procs)))
            n_tries.append(1)
            self.bcrham_proc_info.append({})

        # keep looping over the procs until they're all done
        while procs.count(None) != len(procs):  # we set each proc to None when it finishes
            for iproc in range(n_procs):
                if procs[iproc] is None:  # already finished
                    continue
                if procs[iproc].poll() is not None:  # it's finished
                    utils.finish_process(iproc, procs, n_tries, self.subworkdir(iproc, n_procs), get_outfname(iproc), get_cmd_str(iproc), self.bcrham_proc_info[iproc])
            sys.stdout.flush()
            time.sleep(1)

        print '      time waiting for bcrham: %.1f' % (time.time()-start)
        self.check_wait_times(time.time()-start)
        sys.stdout.flush()

    # ----------------------------------------------------------------------------------------
    def run_hmm(self, algorithm, parameter_in_dir, parameter_out_dir='', count_parameters=False, n_procs=None, precache_all_naive_seqs=False, cpath=None):
        """ 
        Run bcrham, possibly with many processes, and parse and interpret the output.
        NOTE the local <n_procs>, which overrides the one from <self.args>
        """
        start = time.time()
        print 'hmm'
        if len(self.sw_info['queries']) == 0:
            print '  %s no input queries for hmm' % utils.color('red', 'warning')
            return

        if n_procs is None:
            n_procs = self.args.n_procs

        self.write_hmm_input(algorithm, parameter_in_dir, cpath)  # TODO don't keep rewriting it

        cmd_str = self.get_hmm_cmd_str(algorithm, self.hmm_infname, self.hmm_outfname, parameter_dir=parameter_in_dir, precache_all_naive_seqs=precache_all_naive_seqs, n_procs=n_procs)

        if n_procs > 1:
            self.split_input(n_procs, self.hmm_infname)

        self.execute(cmd_str, n_procs)

        new_cpath = self.read_hmm_output(n_procs, count_parameters, parameter_out_dir, precache_all_naive_seqs)
        print '      hmm step time: %.1f' % (time.time()-start)
        return new_cpath

    # ----------------------------------------------------------------------------------------
    def read_cachefile(self):
        """ a.t.m. just want to know which values we have """
        cachefo = {}
        if not os.path.exists(self.hmm_cachefname):
            return cachefo
        with open(self.hmm_cachefname) as cachefile:
            reader = csv.DictReader(cachefile)
            for line in reader:
                cachefo[line['unique_ids']] = {}
        return cachefo

    # ----------------------------------------------------------------------------------------
    def get_expected_number_of_forward_calculations(self, info, namekey, seqkey):
        start = time.time()
        def join_names(name1, name2):  # mimics function in glomeraor.cc
            sortedlist = sorted([name1, name2])
            return ':'.join(sortedlist)

        naive_seqs = self.get_sw_naive_seqs(info, namekey)
        cachefo = self.read_cachefile()
        n_total, n_cached = 0, 0
        for id_a, id_b in itertools.combinations(naive_seqs.keys(), 2):
            seq_a, seq_b = naive_seqs[id_a], naive_seqs[id_b]
            hfrac = utils.hamming_fraction(seq_a, seq_b)
            if hfrac >= self.args.hamming_fraction_bounds[0] and hfrac <= self.args.hamming_fraction_bounds[1]:  # NOTE not sure the equals match up exactly with what's in ham, but it's an estimate, so it doesn't matter
                n_total += 1
                if join_names(id_a, id_b) in cachefo:
                    n_cached += 1
                    assert ':'.join(sorted([id_a, id_b], reverse=True)) not in cachefo
                    assert id_a in cachefo
                    assert id_b in cachefo

        print 'expected total: %d  (cached: %d) --> %d' % (n_total, n_cached, n_total - n_cached)
        print '      expected calc time: %.1f' % (time.time()-start)
        return n_total - n_cached

    # ----------------------------------------------------------------------------------------
    def get_padded_true_naive_seq(self, qry):
        true_naive_seq = self.reco_info[qry]['naive_seq']
        padleft = self.sw_info[qry]['padded']['padleft']  # we're padding the *naive* seq corresponding to qry now, but it'll be the same length as the qry seq
        padright = self.sw_info[qry]['padded']['padright']
        assert len(utils.ambiguous_bases) == 1  # could allow more than one, but it's not implemented a.t.m.
        true_naive_seq = padleft * utils.ambiguous_bases[0] + true_naive_seq + padright * utils.ambiguous_bases[0]
        return true_naive_seq

    # ----------------------------------------------------------------------------------------
    def get_padded_sw_naive_seq(self, qry):
        sw_naive_seq = self.sw_info[qry]['naive_seq']
        padleft = self.sw_info[qry]['padded']['padleft']  # we're padding the *naive* seq corresponding to qry now, but it'll be the same length as the qry seq
        padright = self.sw_info[qry]['padded']['padright']
        assert len(utils.ambiguous_bases) == 1  # could allow more than one, but it's not implemented a.t.m.
        sw_naive_seq = padleft * utils.ambiguous_bases[0] + sw_naive_seq + padright * utils.ambiguous_bases[0]
        return sw_naive_seq

    # ----------------------------------------------------------------------------------------
    def get_sw_naive_seqs(self, info, namekey):

        naive_seqs = {}
        for line in info:
            query = line[namekey]
            if len(query.split(':')) == 1:  # ...but if we don't have them, use smith-waterman (should only be for single queries)
               naive_seqs[query] = self.get_padded_sw_naive_seq(query)
            elif len(query.split(':')) > 1:
                naive_seqs[query] = self.get_padded_sw_naive_seq(query.split(':')[0])  # just arbitrarily use the naive seq from the first one. This is ok partly because if we cache the logprob but not the naive seq, that's because we thought about merging two clusters but did not -- so they're naive seqs should be similar. Also, this is just for divvying queries.
            else:
                raise Exception('no naive sequence found for ' + str(query))
            if naive_seqs[query] == '':
                raise Exception('zero-length naive sequence found for ' + str(query))
        return naive_seqs

    # ----------------------------------------------------------------------------------------
    def split_input(self, n_procs, infname):

        # should we pull out the seeded clusters, and carefully re-inject them into each process?
        separate_seeded_clusters = self.args.seed_unique_id is not None and not (self.already_removed_unseeded_clusters or self.time_to_remove_unseeded_clusters)  # I think I ony actually need one of the latter bools

        # read single input file
        info = []
        seeded_clusters = {}
        with opener('r')(infname) as infile:
            reader = csv.DictReader(infile, delimiter=' ')
            for line in reader:
                if separate_seeded_clusters and self.args.seed_unique_id in set(line['names'].split(':')):
                    if len(seeded_clusters) > 0 and ':' not in line['names']:  # the first time through, we add the seed uid to *every* process. So, when we read those results back in, the procs that didn't merge the seed with anybody will have it as a singleton still, and we only need the singleton once
                        continue
                    seeded_clusters[line['names']] = line
                    continue  # don't want the seeded clusters mixed in with the non-seeded clusters just yet (see below)
                info.append(line)

        # find the smallest seeded cluster
        if separate_seeded_clusters:
            if len(seeded_clusters) == 0:
                raise Exception('couldn\'t find info for seed query %s in %s' % (self.args.seed_unique_id, infname))
            smallest_seed_cluster_str = None
            for unique_id_str in seeded_clusters:
                if smallest_seed_cluster_str is None or len(unique_id_str.split(':')) < len(smallest_seed_cluster_str.split(':')):
                    smallest_seed_cluster_str = unique_id_str

        # ----------------------------------------------------------------------------------------
        def get_sub_outfile(siproc, mode):
            subworkdir = self.subworkdir(siproc, n_procs)
            if mode == 'w':
                utils.prep_dir(subworkdir)
                if os.path.exists(self.hmm_cachefname):  # copy cachefile to this subdir
                    check_call(['cp', self.hmm_cachefname, subworkdir + '/'])
            return open(subworkdir + '/' + os.path.basename(infname), mode)

        # ----------------------------------------------------------------------------------------
        def get_writer(sub_outfile):
            return csv.DictWriter(sub_outfile, reader.fieldnames, delimiter=' ')

        # initialize output files
        for iproc in range(n_procs):
            sub_outfile = get_sub_outfile(iproc, 'w')
            get_writer(sub_outfile).writeheader()
            sub_outfile.close()  # can't leave 'em all open the whole time 'cause python has the thoroughly unreasonable idea that one oughtn't to have thousands of files open at once

        # self.get_expected_number_of_forward_calculations(info, 'names', 'seqs')  # I think this didn't work that well

        seed_clusters_to_write = seeded_clusters.keys()  # the keys in <seeded_clusters> that we still need to write
        for iproc in range(n_procs):
            sub_outfile = get_sub_outfile(iproc, 'a')
            writer = get_writer(sub_outfile)

            # first deal with the seeded clusters
            if separate_seeded_clusters:  # write the seed info line to each file
                if len(seed_clusters_to_write) > 0:
                    if iproc < n_procs - 1:  # if we're not on the last proc, pop off and write the first one
                        writer.writerow(seeded_clusters[seed_clusters_to_write.pop(0)])
                    else:
                        while len(seed_clusters_to_write) > 0:  # keep adding 'em until we run out
                            writer.writerow(seeded_clusters[seed_clusters_to_write.pop(0)])
                else:  # if we don't have any more that we *need* to write (i.e. that have other seqs in them), just write the shortest one (which will frequently be a singleton)
                    writer.writerow(seeded_clusters[smallest_seed_cluster_str])

            # then loop over the non-seeded clusters
            for iquery in range(len(info)):
                if iquery % n_procs != iproc:
                    continue
                writer.writerow(info[iquery])
            sub_outfile.close()

    # ----------------------------------------------------------------------------------------
    def merge_subprocess_files(self, fname, n_procs, include_outfile=False):
        subfnames = []
        for iproc in range(n_procs):
            subfnames.append(self.subworkdir(iproc, n_procs) + '/' + os.path.basename(fname))
        if include_outfile:  # also merge the output file <fname> (i.e. for the cache files, the sub files only include *new* information, so we need to also merge them with the original file)
            subfnames.append(fname)
        self.merge_files(subfnames, fname, dereplicate=False)

    # ----------------------------------------------------------------------------------------
    def merge_files(self, infnames, outfname, dereplicate):
        """ 
        Merge <infnames> into <outfname>.
        NOTE that <outfname> is overwritten with the zero-length file if it exists, otherwise it is created.
        Some of <infnames> may not exist.
        """
        # check_call(['wc', ] + [fn for fn in infnames if fn != outfname])
        # if os.path.exists(outfname):
        #     check_call(['wc', outfname])
        # else:
        #     print '  outfname d.n.e.'

        header = ''
        outfile = None
        one_real_file = False
        if outfname not in infnames or not os.path.exists(outfname):  # if it *is* in <infnames> we assume we can just tack the other infnames onto the end of it and use <outfname>'s header
            outfile = open(outfname, 'w')
        for fname in infnames:
            if not os.path.exists(fname) or os.stat(fname).st_size == 0:
                continue
            one_real_file = True
            with open(fname) as headfile:
                reader = csv.DictReader(headfile)
                header = ','.join(reader.fieldnames)
                if outfile is not None:
                    writer = csv.DictWriter(outfile, reader.fieldnames)
                    writer.writeheader()
            break  # kinda weird to do it this way, but we just need one of the infiles to get the header info (and some may be zero length)
        if outfile is not None:
            outfile.close()
        if not one_real_file:
            print '    nothing to merge into %s' % outfname
            return

        assert header != ''

        cmd = 'cat ' + ' '.join([fn for fn in infnames if fn != outfname]) + ' | grep -v \'' + header + '\''
        cmd += ' >>' + outfname
        try:
            check_call(cmd, shell=True)
        except CalledProcessError:
            print '    nothing to merge into %s' % outfname
            # raise Exception('only read headers from %s', ' '.join([fn for fn in infnames if fn != outfname]))

        if dereplicate:
            tmpfname = outfname + '.tmp'
            check_call('echo ' + header + ' >' + tmpfname, shell=True)
            check_call('grep -v \'' + header + '\' ' + outfname + ' | sort | uniq >>' + tmpfname, shell=True)
            check_call(['mv', '-v', tmpfname, outfname])

        if not self.args.no_clean:
            for infname in infnames:
                if infname != outfname:
                    os.remove(infname)

    # ----------------------------------------------------------------------------------------
    def merge_all_hmm_outputs(self, n_procs, precache_all_naive_seqs):
        """ Merge any/all output files from subsidiary bcrham processes """
        cpath = None  # TODO figure out a cleaner way to do this
        if self.args.action == 'partition':  # merge partitions from several files
            if n_procs > 1:
                self.merge_subprocess_files(self.hmm_cachefname, n_procs, include_outfile=True)  # sub cache files only have new info

            if not precache_all_naive_seqs:
                if n_procs == 1:
                    infnames = [self.hmm_outfname, ]
                else:
                    infnames = [self.subworkdir(iproc, n_procs) + '/' + os.path.basename(self.hmm_outfname) for iproc in range(n_procs)]
                glomerer = Glomerator(self.reco_info, seed_unique_id=self.args.seed_unique_id)
                glomerer.read_cached_agglomeration(infnames, debug=self.args.debug)  #, outfname=self.hmm_outfname)
                assert len(glomerer.paths) == 1
                cpath = glomerer.paths[0]
        else:
            self.merge_subprocess_files(self.hmm_outfname, n_procs)

        if not self.args.no_clean:
            if n_procs == 1:
                os.remove(self.hmm_outfname)
            else:
                for iproc in range(n_procs):
                    subworkdir = self.subworkdir(iproc, n_procs)
                    os.remove(subworkdir + '/' + os.path.basename(self.hmm_infname))
                    if os.path.exists(subworkdir + '/' + os.path.basename(self.hmm_outfname)):
                        os.remove(subworkdir + '/' + os.path.basename(self.hmm_outfname))
                    os.rmdir(subworkdir)

        return cpath

    # ----------------------------------------------------------------------------------------
    def write_hmms(self, parameter_dir):
        """ Write hmm model files to <parameter_dir>/hmms, using information from <parameter_dir> """
        print '  writing hmms with info from %s' % parameter_dir
        # start = time.time()
        from hmmwriter import HmmWriter
        hmm_dir = parameter_dir + '/hmms'
        utils.prep_dir(hmm_dir, '*.yaml')

        if self.args.only_genes is None:  # make a list of all the genes for which we have counts in <parameter_dir> (a.tm., this is all the genes that appeared as a best match at least once)
            gene_list = []
            for region in utils.regions:
                with opener('r')(parameter_dir + '/' + region + '_gene-probs.csv') as pfile:
                    reader = csv.DictReader(pfile)
                    for line in reader:
                        gene_list.append(line[region + '_gene'])
        else:
            gene_list = self.args.only_genes

        for gene in gene_list:
            if self.args.debug:
                print '  %s' % utils.color_gene(gene)
            writer = HmmWriter(parameter_dir, hmm_dir, gene, self.args.naivety, self.glfo, self.args)
            writer.write()

        # print '    time to write hmms: %.1f' % (time.time()-start)

    # ----------------------------------------------------------------------------------------
    def get_existing_hmm_files(self, parameter_dir):
        fnames = [os.path.basename(fn) for fn in glob.glob(parameter_dir + '/hmms/*.yaml')]
        genes = set([utils.unsanitize_name(os.path.splitext(fn)[0]) for fn in fnames])
        if len(genes) == 0:
            raise Exception('no yamels in %s' % parameter_dir + '/hmms')
        return genes

    # ----------------------------------------------------------------------------------------
    def remove_genes_with_no_hmm(self, gene_list, skipped_gene_matches, genes_with_hmm_files):
        """ Check if hmm model file exists, and if not remove gene from <gene_list> """

        # first get the list of genes for which we don't have hmm files
        genes_to_remove = []  # NOTE there should *only* be genes to remove if we're caching parameters, i.e. if we just ran sw for the first time, so we couldn't tell sw ahead of time which genes to use because we didn't know yet
        for gene in gene_list:
            if gene not in genes_with_hmm_files:
                skipped_gene_matches.add(gene)
                genes_to_remove.append(gene)

        # NOTE that we should be removing genes *only* if we're caching parameters, i.e. if we just ran sw on a data set for the first time.
        # The issue is that when we first run sw on a data set, it uses all the genes in self.args.datadir.
        # We then write HMMs for only the genes which were, at least once, a *best* match.
        # But when we're writing the HMM input, we have the N best genes for each sequence, and some of these may not have been a best match at least once.
        # In subsequent runs, however, we already have a parameter dir, so before we run sw we look and see which HMMs we have, and tell sw to only use those, so in this case we shouldn't be removing any.

        # then remove 'em from <gene_list>
        for gene in genes_to_remove:
            gene_list.remove(gene)

    # ----------------------------------------------------------------------------------------
    def all_regions_present(self, gene_list, skipped_gene_matches, query_name, second_query_name=None):
        """ Check that we have at least one gene for each region """
        for region in utils.regions:
            if 'IGH' + region.upper() not in ':'.join(gene_list):
                print '       no %s genes in %s for %s %s' % (region, ':'.join(gene_list), query_name, '' if (second_query_name == None) else second_query_name)
                print '          skipped %s' % (':'.join(skipped_gene_matches))
                print 'giving up on query'
                return False

        return True

    # ----------------------------------------------------------------------------------------
    def combine_queries(self, query_names, genes_with_hmm_files, skipped_gene_matches=None):
        """ 
        Return the 'logical OR' of the queries in <query_names>, i.e. the maximal extent in k_v/k_d space and OR of only_gene sets.
        """

        combo = {
            'k_v':{'min':99999, 'max':-1},
            'k_d':{'min':99999, 'max':-1},
            'only_genes':[],
            'seqs':[],
            'mute-freqs':[],
            'cyst_positions':[]
        }

        # TODO this whole thing probably ought to use cached hmm info if it's available
        # TODO this just always uses the SW mutation rate, but I should really update it with the (multi-)hmm-derived ones (same goes for k space boundaries)

        for name in query_names:
            swfo = self.sw_info[name]
            if 'padded' in swfo:
                k_v = swfo['padded']['k_v']
                seq = swfo['padded']['seq']
                cpos = swfo['padded']['cyst_position']
            else:
                k_v = swfo['k_v']
                seq = swfo['seq']
                cpos = swfo['cyst_position']
            k_d = swfo['k_d']  # don't need to adjust k_d for padding
            combo['seqs'].append(seq)
            combo['mute-freqs'].append(utils.get_mutation_rate(self.glfo['seqs'], swfo))
            combo['cyst_positions'].append(cpos)  # TODO use cached hmm values instead of SW
            combo['k_v']['min'] = min(k_v['min'], combo['k_v']['min'])
            combo['k_v']['max'] = max(k_v['max'], combo['k_v']['max'])
            combo['k_d']['min'] = min(k_d['min'], combo['k_d']['min'])
            combo['k_d']['max'] = max(k_d['max'], combo['k_d']['max'])

            # work out which genes to tell the hmm to use
            only_genes = swfo['all'].split(':')  # start with all the sw matches for this query
            self.remove_genes_with_no_hmm(only_genes, skipped_gene_matches, genes_with_hmm_files)  # remove the ones for which we don't have hmm files (we only write hmms for genes that appeared as the best sw match for at least one query, but swfo['all'] in general includes genes that were never the *best* match for any one query)
            genes_to_use = []
            for region in utils.regions:  # take the best <self.args.n_max_per_region> from each region
                reg_genes = [g for g in only_genes if utils.get_region(g) == region]
                n_genes = min(len(reg_genes), self.args.n_max_per_region[utils.regions.index(region)])  # minimum of [the number of gene matches for this region] and [the number we want for this region]
                for ig in range(n_genes):  # take the first <n_genes> matches (they're ordered by sw match score)
                    genes_to_use.append(reg_genes[ig])

            # and finally OR this query's genes into the ones from previous queries
            combo['only_genes'] = list(set(genes_to_use) | set(combo['only_genes']))  # NOTE using the OR of all sets of genes (from all query seqs) like this *really* helps,

        if not self.all_regions_present(combo['only_genes'], skipped_gene_matches, query_names):
            return {}

        return combo

    # ----------------------------------------------------------------------------------------
    def write_fake_cache_file(self, nsets):
        """ Write a fake cache file which, instead of the inferred naive sequences, has the *true* naive sequences. Used to generate synthetic partitions. """

        if os.path.exists(self.hmm_cachefname):
            print '      cache file exists, not writing fake true naive seqs'
            return

        headers = ['unique_ids', 'logprob', 'naive_seq', 'naive_hfrac', 'cyst_position', 'errors']  # these have to match whatever bcrham is expecting

        print '      caching fake true naive seqs'
        with open(self.hmm_cachefname, 'w') as fakecachefile:
            writer = csv.DictWriter(fakecachefile, headers)
            writer.writeheader()
            for query_name_list in nsets:
                writer.writerow({
                    'unique_ids' : ':'.join([qn for qn in query_name_list]),
                    'naive_seq' : self.get_padded_true_naive_seq(query_name_list[0])  # NOTE just using the first one... but a.t.m. I think I'll only run this fcn the first time through when they're all singletons, anyway
                })

    # ----------------------------------------------------------------------------------------
    def get_seeded_clusters(self, nsets):
        print '      removing unseeded clusters'
        print '         ', ' '.join([':'.join(ns) for ns in nsets if self.args.seed_unique_id in ns])
        seeded_clusters = set()
        for ns in nsets:
            if self.args.seed_unique_id in ns:
                for uid in ns:  # add each individual query that's been clustered with the seed (but split apart)
                    seeded_clusters.add(uid)
        print '         ', ' '.join(seeded_clusters)
        return seeded_clusters

    # ----------------------------------------------------------------------------------------
    def get_unseeded_clusters(self, nsets):
        unseeded_clusters = set()
        for ns in nsets:
            if self.args.seed_unique_id not in ns:
                assert len(ns) == 1
                uid = ns[0]
                unseeded_clusters.add(uid)
        return unseeded_clusters

    # ----------------------------------------------------------------------------------------
    def write_to_single_input_file(self, fname, nsets, parameter_dir, skipped_gene_matches):
        csvfile = opener('w')(fname)
        header = ['names', 'k_v_min', 'k_v_max', 'k_d_min', 'k_d_max', 'only_genes', 'seqs', 'mute_freqs']
        writer = csv.DictWriter(csvfile, header, delimiter=' ')  # TODO should eventually rewrite arg parser in ham to handle csvs (like in glomerator cache reader)
        writer.writeheader()

        if not self.args.no_random_divvy:  # shuffle nset order (this is important because we want the calculations to be spread uniformly among the n processes)
            random.shuffle(nsets)

        if self.args.synthetic_distance_based_partition:
            self.write_fake_cache_file(nsets)

        genes_with_hmm_files = self.get_existing_hmm_files(parameter_dir)

        for query_name_list in nsets:  # NOTE in principle I think I should remove duplicate singleton <seed_unique_id>s here. But I think they in effect get removed 'cause in bcrham everything's stored as hash maps, so any duplicates just overwites the original upon reading its input
            combined_query = self.combine_queries(query_name_list, genes_with_hmm_files, skipped_gene_matches=skipped_gene_matches)
            if len(combined_query) == 0:  # didn't find all regions
                continue
            writer.writerow({
                'names' : ':'.join([qn for qn in query_name_list]),
                'k_v_min' : combined_query['k_v']['min'],
                'k_v_max' : combined_query['k_v']['max'],
                'k_d_min' : combined_query['k_d']['min'],
                'k_d_max' : combined_query['k_d']['max'],
                'only_genes' : ':'.join(combined_query['only_genes']),
                'seqs' : ':'.join(combined_query['seqs']),
                'mute_freqs' : ':'.join([str(f) for f in combined_query['mute-freqs']]),
            })

        csvfile.close()

    # ----------------------------------------------------------------------------------------
    def write_hmm_input(self, algorithm, parameter_dir, cpath):
        """ Write input file for bcrham """
        print '    writing input'

        skipped_gene_matches = set()

        if self.args.action == 'partition' and algorithm == 'forward':  # if we're caching naive seqs before partitioning, we're doing viterbi (and want the block below)
            nsets = copy.deepcopy(cpath.partitions[cpath.i_best_minus_x])  # NOTE that a.t.m. i_best and i_best_minus_x are the same, since we're not calculating log probs of partitions (well, we're trying to avoid calculating any extra log probs, which means we usually don't know the log prob of the entire partition)
            if self.args.seed_unique_id is not None and self.time_to_remove_unseeded_clusters:
                nsets = [[qr] for qr in self.get_seeded_clusters(nsets)]
                self.already_removed_unseeded_clusters = True
        else:
            if self.args.n_sets == 1:  # single (non-multi) hmm (does the same thing as the below for n=1, but is more transparent)
                nsets = [[qn] for qn in self.sw_info['queries']]
            else:
                if self.args.all_combinations:  # run on *every* combination of queries which has length <self.args.n_sets>
                    nsets = itertools.combinations(self.sw_info['queries'], self.args.n_sets)
                else:  # put the first n together, and the second group of n, and so forth (note that self.sw_info['queries'] is a list)
                    nsets = []
                    keylist = [k for k in self.input_info.keys() if k in self.sw_info['queries']]  # we want the queries from sw (to skip failures), but the order from input_info
                    this_set = []
                    for iquery in range(len(keylist)):
                        if iquery % self.args.n_sets == 0:  # every nth query, start a new group
                            if len(this_set) > 0:
                                nsets.append(this_set)
                            this_set = []
                        this_set.append(keylist[iquery])
                    if len(this_set) > 0:
                        nsets.append(this_set)

        self.write_to_single_input_file(self.hmm_infname, nsets, parameter_dir, skipped_gene_matches)

        if self.args.debug and len(skipped_gene_matches) > 0:
            print '    not found in %s, so removing from consideration for hmm (i.e. were only the nth best, but never the best sw match for any query):' % (parameter_dir),
            for region in utils.regions:
                # print '  %s: %d' % (region, len([gene for gene in skipped_gene_matches if utils.get_region(gene) == region])),
                print '\n      %s: %s' % (region, ' '.join([utils.color_gene(gene) for gene in sorted(skipped_gene_matches) if utils.get_region(gene) == region]))
            print ''

    # ----------------------------------------------------------------------------------------
    def read_hmm_output(self, n_procs, count_parameters, parameter_out_dir, precache_all_naive_seqs):
        cpath = None  # TODO figure out a cleaner way to do this
        if self.args.action == 'partition' or n_procs > 1:
            cpath = self.merge_all_hmm_outputs(n_procs, precache_all_naive_seqs)

        if self.args.action != 'partition':
            if self.args.action == 'run-viterbi' or self.args.action == 'cache-parameters':
                self.read_annotation_output(self.hmm_outfname, count_parameters=count_parameters, parameter_out_dir=parameter_out_dir, outfname=self.args.outfname)
            elif self.args.action == 'run-forward':
                self.read_forward_output(self.hmm_outfname)

        if not self.args.no_clean and os.path.exists(self.hmm_infname):
            os.remove(self.hmm_infname)

        return cpath

    # ----------------------------------------------------------------------------------------
    def check_for_bcrham_failures(self, line, boundary_error_queries):
        if 'no_path' in line['errors']:
            self.bcrham_failed_queries.add(line['unique_ids'])
            return True
        if line['nth_best'] == 0:  # if this is the first line for this set of uids (i.e. the best viterbi path or only forward score)
            if line['errors'] is not None and 'boundary' in line['errors'].split(':'):
                boundary_error_queries.append(':'.join([uid for uid in line['unique_ids']]))
            else:  # we don't expect anything except boundary errors a.t.m.
                assert len(line['errors']) == 0

    # ----------------------------------------------------------------------------------------
    def read_forward_output(self, annotation_fname):
        probs = OrderedDict()
        with opener('r')(annotation_fname) as csvfile:
            reader = csv.DictReader(csvfile)
            for line in reader:
                if line['errors'] != '':
                    print '  bcrham errors (%s) for %s' % (line['errors'], line['unique_ids'])
                probs[line['unique_ids']] = float(line['logprob'])

        if self.args.outfname is not None:
            with open(self.args.outfname, 'w') as outfile:
                writer = csv.DictWriter(outfile, ('unique_ids', 'logprob'))
                writer.writeheader()
                for uids, prob in probs.items():
                    writer.writerow({'unique_ids' : uids, 'logprob' : prob})

        if not self.args.no_clean:
            os.remove(annotation_fname)

    # ----------------------------------------------------------------------------------------
    def read_annotation_output(self, annotation_fname, outfname=None, count_parameters=False, parameter_out_dir=None):
        """ Read bcrham annotation output """
        print '    read output'

        pcounter = ParameterCounter(self.glfo['seqs']) if count_parameters else None
        true_pcounter = ParameterCounter(self.glfo['seqs']) if (count_parameters and not self.args.is_data) else None
        perfplotter = PerformancePlotter(self.glfo['seqs'], 'hmm') if self.args.plot_performance else None

        n_lines_read, n_seqs_processed, n_events_processed, n_invalid_events = 0, 0, 0, 0
        padded_annotations, eroded_annotations = OrderedDict(), OrderedDict()
        boundary_error_queries = []
        with opener('r')(annotation_fname) as hmm_csv_outfile:
            reader = csv.DictReader(hmm_csv_outfile)
            for padded_line in reader:  # line coming from hmm output is N-padded such that all the seqs are the same length

                n_lines_read += 1

                failed = self.check_for_bcrham_failures(padded_line, boundary_error_queries)
                if failed:
                    continue

                utils.process_input_line(padded_line)
                uids = padded_line['unique_ids']
                padded_line['indelfos'] = [self.sw_info['indels'].get(uid, utils.get_empty_indel()) for uid in uids]

                utils.add_implicit_info(self.glfo, padded_line, multi_seq=True)
                if padded_line['invalid']:
                    n_invalid_events += 1
                    if self.args.debug:
                        print '      %s padded line invalid' % ':'.join(padded_line['unique_ids'])
                    continue

                # get a new dict in which we have edited the sequences to swap Ns on either end (after removing fv and jf insertions) for v_5p and j_3p deletions
                eroded_line = utils.reset_effective_erosions_and_effective_insertions(self.glfo, padded_line)
                if eroded_line['invalid']:  # not really sure why the eroded line is sometimes invalid when the padded line is not, but it's very rare and I don't really care, either
                    n_invalid_events += 1
                    continue

                padded_annotations[':'.join(padded_line['unique_ids'])] = padded_line
                eroded_annotations[':'.join(padded_line['unique_ids'])] = eroded_line

                if self.args.debug:
                    if padded_line['nth_best'] == 0:  # if this is the first padded_line (i.e. the best viterbi path) for this query (or query pair), print the true event
                        print '      %s' % ':'.join(uids),
                        if not self.args.is_data:
                            print '   %d' % utils.from_same_event(self.reco_info, uids),
                        print ''
                    self.print_hmm_output(eroded_line, print_true=(eroded_line['nth_best']==0))

                if padded_line['nth_best'] == 0:  # if it's the best match  #  NOTE kinda nervous about removing this: and (padded_line['cdr3_length'] != -1 or not self.args.skip_unproductive):  # if it's productive, or if we're not skipping unproductive rearrangements

                    n_events_processed += 1

                    if pcounter is not None:
                        pcounter.increment_per_family_params(eroded_line)
                    if true_pcounter is not None:
                        true_pcounter.increment_per_family_params(self.reco_info[uids[0]])  # NOTE doesn't matter which id you pass it, since they all have the same reco parameters

                    for iseq in range(len(uids)):
                        singlefo = utils.synthesize_single_seq_line(eroded_line, iseq)
                        if pcounter is not None:
                            pcounter.increment_per_sequence_params(singlefo)
                        if true_pcounter is not None:
                            true_pcounter.increment_per_sequence_params(self.reco_info[uids[iseq]])
                        if perfplotter is not None:
                            if uids[iseq] in self.sw_info['indels']:
                                print '    skipping performance evaluation of %s because of indels' % uids[iseq]  # I just have no idea how to handle naive hamming fraction when there's indels
                            else:
                                perfplotter.evaluate(self.reco_info[uids[iseq]], singlefo, self.sw_info[uids[iseq]]['padded'])
                        n_seqs_processed += 1

        # parameter and performance writing/plotting
        if pcounter is not None:
            pcounter.write(parameter_out_dir)
            if self.args.plotdir is not None:
                pcounter.plot(self.args.plotdir + '/hmm', subset_by_gene=True, cyst_positions=self.glfo['cyst-positions'], tryp_positions=self.glfo['tryp-positions'], only_csv=self.args.only_csv_plots)
        if true_pcounter is not None:
            true_pcounter.write(parameter_out_dir + '-true')
            if self.args.plotdir is not None:
                true_pcounter.plot(self.args.plotdir + '/hmm-true', subset_by_gene=True, cyst_positions=self.glfo['cyst-positions'], tryp_positions=self.glfo['tryp-positions'], only_csv=self.args.only_csv_plots)
        if perfplotter is not None:
            perfplotter.plot(self.args.plotdir + '/hmm', only_csv=self.args.only_csv_plots)

        print '        %d lines:  processed %d sequences in %d events (skipped %d invalid events)' % (n_lines_read, n_seqs_processed, n_events_processed, n_invalid_events)
        if len(self.bcrham_failed_queries) > 0:
            print '      no valid paths: %s' % ':'.join(self.bcrham_failed_queries)
        if len(boundary_error_queries) > 0:
            print '      %d boundary errors' % len(boundary_error_queries)
            if self.args.debug:
                print '                %s' % ', '.join(boundary_error_queries)

        # write output file
        if outfname is not None:
            self.write_annotations(eroded_annotations, outfname)

        # annotation (VJ CDR3) clustering
        if self.args.annotation_clustering is not None:
            self.deal_with_annotation_clustering(eroded_annotations, outfname)

        if not self.args.no_clean:
            os.remove(annotation_fname)

        return eroded_annotations

    # ----------------------------------------------------------------------------------------
    def deal_with_annotation_clustering(self, annotations, outfname):
        if self.args.annotation_clustering != 'vollmers':
            raise Exception('we only handle \'vollmers\' (vj cdr3 0.x) annotation clustering at the moment')

        # initialize output file
        if outfname is not None:
            outfile = open(outfname, 'w')  # NOTE overwrites annotation info that's already been written to <outfname>
            headers = ['n_clusters', 'threshold', 'partition']
            if not self.args.is_data:
                headers += ['ccf_under', 'ccf_over']
            writer = csv.DictWriter(outfile, headers)
            writer.writeheader()

        # have to copy info to new dict to get d_qr_seq and whatnot
        annotations_for_vollmers = OrderedDict()
        for uidstr, line in annotations.items():
            if len(line['seqs']) > 1:
                raise Exception('can\'t handle multiple seqs')
            annotations_for_vollmers[uidstr] = utils.synthesize_single_seq_line(line, iseq)

        # perform annotation clustering for each threshold and write to file
        for thresh in self.args.annotation_clustering_thresholds:
            partition = annotationclustering.vollmers(annotations_for_vollmers, threshold=thresh, reco_info=self.reco_info)
            n_clusters = len(partition)
            if outfname is not None:
                row = {'n_clusters' : n_clusters, 'threshold' : thresh, 'partition' : utils.get_str_from_partition(partition)}
                if not self.args.is_data:
                    true_partition = utils.get_true_partition(self.reco_info)
                    ccfs = utils.new_ccfs_that_need_better_names(partition, true_partition, self.reco_info)
                    row['ccf_under'] = ccfs[0]
                    row['ccf_over'] = ccfs[1]
                writer.writerow(row)

        if outfname is not None:
            outfile.close()

    # ----------------------------------------------------------------------------------------
    def print_hmm_output(self, line, print_true=False):
        if print_true and not self.args.is_data:  # first print true event (if this is simulation)
            utils.print_true_events(self.glfo, self.reco_info, line)

        utils.print_reco_event(self.glfo['seqs'], line, extra_str='    ', label='inferred:')

    # ----------------------------------------------------------------------------------------
    def print_performance_info(self, line, perfplotter=None):
        true_line = self.reco_info[line['unique_id']]
        genes_ok = ['ok'  if (line[region+'_gene'] == true_line[region+'_gene']) else 'no' for region in utils.regions]
        print '         v  d  j   hamming      erosions      insertions'
        print '        %3s%3s%3s' % tuple(genes_ok),
        print '  %3d' % (perfplotter.hamming_distance_to_true_naive(true_line, line, line['unique_id']) if perfplotter != None else -1),
        print '   %4d%4d%4d%4d' % tuple([int(line[ero+'_del']) - int(true_line[ero+'_del']) for ero in utils.real_erosions]),
        print '   %4d%4d' % tuple([len(line[bound+'_insertion']) - len(true_line[bound+'_insertion']) for bound in utils.boundaries])

    # ----------------------------------------------------------------------------------------
    def write_annotations(self, annotations, outfname):
        outpath = outfname
        if outpath[0] != '/':  # if full output path wasn't specified on the command line
            outpath = os.getcwd() + '/' + outpath
        outheader = ['unique_ids', 'v_gene', 'd_gene', 'j_gene', 'cdr3_length', 'seqs', 'aligned_v_seqs', 'aligned_d_seqs', 'aligned_j_seqs', 'naive_seq', 'indelfos']
        outheader += [e + '_del' for e in utils.real_erosions + utils.effective_erosions] + [b + '_insertion' for b in utils.boundaries + utils.effective_boundaries]
        outheader += [fc + 's' for fc in utils.functional_columns]
        with open(outpath, 'w') as outfile:
            writer = csv.DictWriter(outfile, utils.presto_headers.values() if self.args.presto_output else outheader)
            writer.writeheader()
            missing_input_keys = set(self.input_info.keys())  # all the keys we originially read from the file
            for line in annotations.values():
                outline = {k : copy.deepcopy(line[k]) for k in outheader}  # in case we modify it
                for uid in outline['unique_ids']:
                    missing_input_keys.remove(uid)

                if self.args.presto_output:
                    outline = utils.convert_to_presto(self.glfo, outline)
                else:
                    outline = utils.get_line_for_output(outline)  # may be kind of silly to replace it, but I don't want to change the original line too much

                writer.writerow(outline)

            # and write empty lines for seqs that failed either in sw or the hmm
            if len(missing_input_keys) > 0:
                print 'missing %d input keys' % len(missing_input_keys)
                for uid in missing_input_keys:
                    writer.writerow({'unique_ids' : uid})
