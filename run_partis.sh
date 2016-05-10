file_map=$SCRATCH/abbrev_map.txt;

fastq_dir=/shahlab/azhang/IX3248.02-presto;
results_dir=/shahlab/azhang/partis_results;
mkdir -p $results_dir/params;
cluster_dir=/shahlab/azhang/clusttmp/partis1;
mkdir -p $cluster_dir;

while IFS= read line
do
    sample=`echo $line | awk '{print $1}'`;
    in_file=$fastq_dir/$sample"_assemble-pass.fastq";
    param_dir=$results_dir/params/$sample;
    jobname="PARTIS_"$sample;
    clust_err_file=$cluster_dir/$sample.err;
    clust_out_file=$cluster_dir/$sample.out;
    ## Must be run from inside this directory; import statements not properly coded
    qsub -l h_vmem=24G,mem_token=24G,mem_free=24G -e $clust_err_file -o $clust_out_file -V -b yes -N $jobname "./bin/partis.py --action cache-parameters --n-procs 1 --seqfile $in_file --parameter-dir $param_dir"
done < $file_map

echo "All jobs submitted."
