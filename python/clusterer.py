import sys
import csv
import math

import utils
from opener import opener
# ./venv/bin/linsim compare-clustering --true-name-column unique_id --inferred-name-column unique_id  --true-group-column reco_id --inferred-group-column reco_id /tmp/dralph/true.csv /tmp/dralph/inf.csv 

class Clusterer(object):
    # ----------------------------------------------------------------------------------------
    def __init__(self, threshold, greater_than=True):  # put in same cluster if greater than threshold, or less than equal to?
        self.threshold = threshold
        self.debug = False
        self.greater_than = greater_than
        self.max_id = -1  # maximum previously used id
        self.cluster_ids = []
        self.query_clusters = {}  # map from query name to cluster id
        self.id_clusters = {}  # map from cluster id to query name list
        self.pairscores = {}  # keep all the scores in memory. TODO may be too large?

        self.nearest_true_mate = {}  # 

    # ----------------------------------------------------------------------------------------
    def cluster(self, infname, debug=False, reco_info=None, outfile=None):
        self.debug = debug
        with opener('r')(infname) as infile:
            reader = csv.DictReader(infile)
            for line in reader:
                query1 = line['unique_id']
                query2 = line['second_unique_id']
                score = float(line['score'])
                if self.debug:
                    print '%22s %22s   %.3f' % (query1, query2, score),
                self.incorporate_into_clusters(query1, query2, score)
                self.pairscores[utils.get_key(query1, query2)] = score
                if reco_info != None and reco_info[query1]['reco_id'] == reco_info[query2]['reco_id']:
                    for query,score in {query1:score, query2:score}.iteritems():
                        if query not in self.nearest_true_mate:
                            self.nearest_true_mate[query] = score
                        elif self.greater_than and score > self.nearest_true_mate[query]:
                            self.nearest_true_mate[query] = score
                        elif not self.greater_than and score < self.nearest_true_mate[query]:
                            self.nearest_true_mate[query] = score
                if self.debug:
                    print ''

        for query, cluster_id in self.query_clusters.iteritems():
            if cluster_id not in self.id_clusters:
                self.id_clusters[cluster_id] = []
            self.id_clusters[cluster_id].append(query)

        # print 'nearest',self.nearest_true_mate
        out_str_list = []
        for cluster_id in self.id_clusters:
            out_str_list.append('   ' + ' '.join(self.id_clusters[cluster_id]) + '\n')
        if outfile == None:
            print ''.join(out_str_list)
        else:
            outfile.write(''.join(out_str_list))

    # ----------------------------------------------------------------------------------------
    def add_new_cluster(self, query_name):
        if self.debug:
            print '    new cluster ',query_name,
        assert query_name not in self.query_clusters
        self.max_id += 1
        self.query_clusters[query_name] = self.max_id
        self.cluster_ids.append(self.max_id)

    # ----------------------------------------------------------------------------------------
    def merge_clusters(self, query_name, second_query_name):
        """ move all queries with same id as <second_query_name> to <query_name>'s cluster """
        if self.query_clusters[query_name] == self.query_clusters[second_query_name]:
            if self.debug:
                print '     already together',
            return
        if self.debug:
            print '     merging ',self.query_clusters[query_name], ' and ',self.query_clusters[second_query_name],
        first_cluster_id = self.query_clusters[query_name]
        second_cluster_id = self.query_clusters[second_query_name]

        if first_cluster_id == second_cluster_id:  # already in the same cluster
            return
        for name,cluster_id in self.query_clusters.iteritems():
            if cluster_id == second_cluster_id:
                self.query_clusters[name] = first_cluster_id

        if second_cluster_id in self.cluster_ids:
            self.cluster_ids.remove(second_cluster_id)
        else:
            print 'oh, man, something\'s wrong'
            print 'uniqe_id,reco_id'
            for name,cluster_id in self.query_clusters.iteritems():
                print '%s,%d' % (name, cluster_id)
            sys.exit()

    # ----------------------------------------------------------------------------------------
    def add_to_cluster(self, cluster_id, query_name):
        if self.debug:
            print '    adding ',query_name,' to ',cluster_id,
        self.query_clusters[query_name] = cluster_id

    # ----------------------------------------------------------------------------------------
    def is_removable(self, score):
        if math.isnan(score):
            assert False
        if self.greater_than:
            return score <= self.threshold
        else:
            return score > self.threshold

    # ----------------------------------------------------------------------------------------
    def incorporate_into_clusters(self, query_name, second_query_name, score):
        if self.is_removable(score):  # TODO this makes singletons not be included in any cluster... I should fix that
            if self.debug:
                print '    removing link',
            return
        if query_name in self.query_clusters and second_query_name in self.query_clusters:  # if both seqs are already in clusters
            self.merge_clusters(query_name, second_query_name)
        elif query_name in self.query_clusters:
            self.add_to_cluster(self.query_clusters[query_name], second_query_name)
        elif second_query_name in self.query_clusters:
            self.add_to_cluster(self.query_clusters[second_query_name], query_name)
        else:
            self.add_new_cluster(query_name)
            self.add_to_cluster(self.query_clusters[query_name], second_query_name)
