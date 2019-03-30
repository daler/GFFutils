#!/bin/bash
set -euo pipefail
set -x
HERE=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
curl -O https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p ~/anaconda
export PATH=~/anaconda/bin:$PATH

conda config --add channels defaults
conda config --add channels conda-forge
conda config --add channels bioconda

conda create -n tmp$PY -y \
    --file $HERE/../requirements.txt \
    --file $HERE/../optional-requirements.txt \
    nose \
    python=$PY

source activate tmp$PY
python setup.py install
source deactivate
