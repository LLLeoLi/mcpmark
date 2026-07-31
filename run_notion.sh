DOCKER_MEMORY_LIMIT=16g ./run-benchmark.sh --models qwen-3-coder-30b-a3b-instruct --exp-name qwen-3-coder-0722-150 --mcps notion --docker --ptc --ptc-timeout 60
./run-benchmark.sh --models qwen-3-coder-30b-a3b-instruct --exp-name qwen-3-coder-0722-150 --mcps notion --docker
# ./run-benchmark.sh --models qwen-3-8b --k 1 --exp-name qwen-3-8b-75 --mcps filesystem,postgres --docker
# ./run-benchmark.sh --models qwen-3.5-9b --k 1 --exp-name qwen-3.5-9b-25-ptc --mcps postgres --docker --ptc --ptc-timeout 240
# ./run-benchmark.sh --models qwen-3.5-9b --k 1 --exp-name qwen-3.5-9b-25 --docker
# ./run-benchmark.sh --models qwen-3-8b --k 1 --exp-name qwen3-coder-sft-ptc  --docker --ptc --ptc-timeout 240 --mcps filesystem,postgres
