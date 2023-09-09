import os
import time
import subprocess

gpu_id = 0  # 你要检查的显卡ID
pid = 143347 # 你要检查的进程ID
# sh path
script_path = "/home/zhuxinming/ProtoFormer-main/train.py"
# 循环等待进程结束
while True:
    cmd = f"nvidia-smi -i {gpu_id} | grep -w {pid}"
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    output, _ = process.communicate()

    if output:
        print("Process is still running.")
        time.sleep(30)
    else:
        print("Process has finished.")
        break

# 在这里运行您的模型代码
os.system(script_path)