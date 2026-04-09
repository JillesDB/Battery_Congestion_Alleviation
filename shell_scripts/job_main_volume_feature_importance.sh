#!/bin/sh
### General options
### -- specify queue --
#BSUB -q man
### -- set the job Name --
#BSUB -J Job_Run_py
### -- ask for number of cores (default: 1) --
#BSUB -n 8
### -- specify that the cores must be on the same host --
#BSUB -R "span[hosts=1]"
### -- specify that we need 4GB of memory per core/slot --
#BSUB -R "rusage[mem=8GB]"
### -- specify that we want the job to get killed if it exceeds 5 GB per core/slot --
#BSUB -M 9GB
### -- set walltime limit: hh:mm --
#BSUB -W 48:00
### -- set the email address --
# please uncomment the following line and put in your e-mail address,
# if you want to receive e-mail notifications on a non-default address
#BSUB -u jcdbl@dtu.dk
### -- send notification at start --
###BSUB -B
### -- send notification at completion --
###BSUB -N
### -- Specify the output and error file. %J is the job-id --
### -- -o and -e mean append, -oo and -eo mean overwrite --
#BSUB -o hpc_output_and_error_files/Output_%J.out
#BSUB -e hpc_output_and_error_files/Output_%J.err

# here follow the commands you want to execute with input.in as the input file
module load python3/3.12.4
# shellcheck disable=SC3046
source /zhome/26/e/209460/PycharmProjects/Bidding_Pattern_Analysis/packages_bidding_pattern_analysis/bin/activate
python3 main_volume_feature_importance.py