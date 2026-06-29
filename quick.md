./run-benchmark.sh --models qwen-3-8b-sft --k 1 --exp-name qwen3-coder-base  --docker
./run-benchmark.sh --models qwen-3-8b-sft --k 1 --exp-name qwen3-coder-base-ptc  --docker --ptc

用法:                                                                                                                                                                                                                   
  - 起:./start-vllm-relay.sh                                                                                                                                                                                                                                                                
  - 停:./start-vllm-relay.sh stop                                                                                                                                                                                                                                                           
  - 改端口:LISTEN_PORT=28026 UPSTREAM_PORT=18025 ./start-vllm-relay.sh(注意如果 LISTEN_PORT 改了,还要同步改 run-task.sh postgres 分支里的 QWEN3_CODER_30B_BASE_URL)