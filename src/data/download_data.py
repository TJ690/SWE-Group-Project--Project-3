from huggingface_hub import snapshot_download

local_dir = snapshot_download(
    repo_id="mvaccargiu/gitskills",
    repo_type="dataset",
    local_dir="./gitskills_data",   # wherever you have space
)
print(local_dir)