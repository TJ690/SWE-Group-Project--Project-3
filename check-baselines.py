import sys
sys.path.insert(0, "src")
import baselines as b

df = b.load_features()
print("rows:", len(df), "(expected 1,877,981)")

labels = b.copy_count(df)
print("templated:", (labels == b.TEMPLATED).sum(), "(expected 35,768)")

print(b.run_all(df).groupby(["baseline", "label"]).size())
