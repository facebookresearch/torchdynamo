#!/usr/bin/env bash

datasets="-k hf_GPT2 -k mnasnet1_0 -k mobilenet_v2 -k resnet50 -k shufflenet_v2_x1_0 -k squeezenet1_1 -k timm_efficientnet -k timm_regnet -k timm_vovnet -k hf_Albert"
repeat=50
fp="float32"
batch_size_file="\$(realpath benchmarks/torchbench_models_list.txt)"

EXP="$1"


if [[ "mem" != "$EXP" ]]; then
    methods=("accuracy-aot-ts" "accuracy-ts") #"accuracy-aot-ts-mincut" 
    for method in "${methods[@]}"; do
        cmd="AOT_PARTITIONER_DEBUG=1 PYTORCH_NVFUSER_DISABLE_FALLBACK=1 python benchmarks/torchbench.py --training --devices=cuda --nvfuser --${method} --use-eval-mode --isolate --${fp} --batch_size_file ${batch_size_file} --repeat $repeat ${datasets}"
        echo "$cmd"
        eval "$cmd"
    done
else
    methods=("aot_nvfuser_nop" "aot_nop" "eager")
    for method in "${methods[@]}"; do
        cmd="AOT_PARTITIONER_DEBUG=1 PYTORCH_NVFUSER_DISABLE_FALLBACK=1 python benchmarks/torchbench.py --training --devices=cuda --use-eval-mode --isolate --${fp} --peak-memory-for-backend=${method} --output=exp.csv --batch_size_file ${batch_size_file} ${datasets}"
        echo "$cmd"
        eval "$cmd"
    done
fi