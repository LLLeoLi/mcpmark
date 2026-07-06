./run-benchmark.sh --models qwen-3-8b --k 1 --exp-name qwen-3-8b-100-ptc --mcps postgres --docker --ptc --ptc-timeout 120
./run-benchmark.sh --models qwen-3-8b --k 1 --exp-name qwen-3-8b-100-ptc --mcps filesystem --docker --ptc --ptc-timeout 60
./run-benchmark.sh --models qwen-3-8b --k 1 --exp-name qwen-3-8b-100-ptc --mcps notion --docker --ptc --ptc-timeout 60
# ./run-benchmark.sh --models qwen-3-8b --k 1 --exp-name qwen-3-8b-75 --mcps filesystem,postgres --docker
# ./run-benchmark.sh --models qwen-3.5-9b --k 1 --exp-name qwen-3.5-9b-25-ptc --mcps postgres --docker --ptc --ptc-timeout 240
# ./run-benchmark.sh --models qwen-3.5-9b --k 1 --exp-name qwen-3.5-9b-25 --docker
# ./run-benchmark.sh --models qwen-3-8b --k 1 --exp-name qwen3-coder-sft-ptc  --docker --ptc --ptc-timeout 240 --mcps filesystem,postgres
