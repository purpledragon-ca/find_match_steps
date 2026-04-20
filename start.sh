#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

exec python match_step_component_positions.py \
    ~/my_workspace/usd_assets/assets/objects/fraction_waste_bin/sources/processed/tube_waste_bucket_centered.step \
    ~/my_workspace/usd_assets/scenes/front_shelf/sources/processed/front_sleft_ori_centered.step \
    --launch-ui "$@"
