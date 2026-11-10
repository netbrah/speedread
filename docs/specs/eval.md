# Evaluation log

Machine: sandbox, 2 vCPU, 7 GiB RAM, Linux x86_64, rustc 1.99.1. Small-machine numbers;
see 00 for what this does and does not validate.

## Thresholds

- Wall time and peak RSS of the baseline query set: no more than +10% versus the previous
  shipped commit (median of 5 runs).
- Output bytes of unchanged commands: identical, unless the spec says otherwise.
- Each feature's own metric: stated in its spec.

## Results

(appended by each shipped slice; raw output in `evals/fork/`)
