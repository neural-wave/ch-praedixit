"""Layer 0: pick subset S (2,000 patients, stratified by file size so large patients are in) and hard-link
the files into data/subset_S. Only file sizes are read, never the patient contents."""
import os
import random

from common import CFG, DATA, HERE

random.seed(CFG["seed"])
src = (HERE / CFG["patients_dir"]).resolve()
files = sorted((e.stat().st_size, e.name) for e in os.scandir(src) if e.name.endswith(".ttl"))
n = CFG["subset_size"]
step = len(files) / n
picked = [random.choice(files[int(i * step):int((i + 1) * step)]) for i in range(n)]

out = DATA / "subset_S"
out.mkdir(exist_ok=True)
for _, name in picked:
    if not (out / name).exists():
        os.link(src / name, out / name)  # no extra disk space

print(f"{n} files linked into {out}, {sum(s for s, _ in picked) / 1e9:.1f} GB, largest {picked[-1][0] / 1e6:.0f} MB")
print("Now load S into its own Virtuoso container (your main container is left alone) by running these 4 lines:")
print("  cd ../hackathon-virtuoso-release")
print("  export CONTAINER_NAME=virtuoso-S HTTP_HOST_PORT=8891 ISQL_HOST_PORT=1112 VIRTUOSO_RAM_GB=8 "
      "PATIENTS_DIR=../generation_folder/data/subset_S")
print("  ./start.sh && ./load-patients.sh")
print("  cd ../generation_folder")
