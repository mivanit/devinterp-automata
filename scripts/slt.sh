#!/bin/bash

# This script can optionally manage the main config name via command line passing to Hydra
SCRIPT_PATH="slt.py"

task="dihedral"
model=TF_LENS

# Whether to log particular things
llc="False"
ed="True"
form="False"

run_idx=0
skip_cps=2

# Hyperparams
lr=5e-4
its=25000
layers=2
seq_len=25

# ED
early_sigma=50
late_sigma=100
marked_cusp_data="None"

for i in {1..1}
do
    HYDRA_FULL_ERROR=1 python $SCRIPT_PATH -m num_training_iter=$its model_type=$model llc=$llc ed=$ed form=$form lr=$lr task_config=$task seq_len=$seq_len n_layers=$layers run_idx=$run_idx ed_plot_config.smoothing_sigma_early=$early_sigma skip_cps=$skip_cps ed_plot_config.smoothing_sigma_late=$late_sigma hydra.job.chdir=True

done