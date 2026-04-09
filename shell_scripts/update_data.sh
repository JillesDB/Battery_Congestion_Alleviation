#!/bin/sh
### General options
### -- specify queue --
#BSUB -q hpc
### -- set the job Name --
#BSUB -J Job_Run_py
### -- ask for number of cores (default: 1) --
#BSUB -n 4
### -- specify that the cores must be on the same host --
#BSUB -R "span[hosts=1]"
### -- specify that we need 4GB of memory per core/slot --
#BSUB -R "rusage[mem=4GB]"
### -- specify that we want the job to get killed if it exceeds 5 GB per core/slot --
#BSUB -M 5GB
### -- set walltime limit: hh:mm --
#BSUB -W 24:00
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
#BSUB -o Output_%J.out
#BSUB -e Output_%J.err

# here follow the commands you want to execute with input.in as the input file
scp -r "\Users\jcdbl\OneDrive - Danmarks Tekniske Universitet\Laptop Folder\DTU - PhD Work\2024.2 Bidding Curve Analysis\EPEX Spot Market Data\belgium\Day-Ahead Auction\Hourly\Historical\Aggregated curves\auction_aggregated_curves_belgium_2020" jcdbl@login1.hpc.dtu.dk:"/zhome/26/e/209460/belgium/Day-Ahead Auction/Hourly/Historical/Aggregated curves/auction_aggregated_curves_belgium_2020"