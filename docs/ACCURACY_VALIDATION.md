# Face verification accuracy validation

Run date: 2026-09-03

This is a local replication of the Labeled Faces in the Wild (LFW) View 2
verification protocol using FaceProof's exact pinned YuNet detector, SFace
recognizer, strict one-face rule, and default quality policy. It measures 1:1
face verification only. It does **not** measure web search, social-media
retrieval, or open-set 1:N identification accuracy.

No LFW image, subject identity, embedding, individual pair score, or individual
result is committed or included in the aggregate output. The local dataset and
generated JSON report live under the git-ignored `benchmark-data/` directory.

## Reproduce

Download and verify the models first, then run:

```powershell
uv run faceproof models download
uv run python scripts/benchmark_lfw.py --download `
  --output benchmark-data/lfw/faceproof-lfw-result.json
```

The downloader verifies the full archive and `pairs.txt` against fixed SHA-256
digests. A clean extraction is tied to that archive, and every subsequent run
rehashes all 13,233 JPEG paths, sizes, and contents. Arbitrary model paths are
accepted only if their contents match the model hashes pinned by FaceProof.
An external LFW directory must match the pinned tree fingerprint, but its
archive acquisition is explicitly reported as unverified.

## Aggregate result

| Measure | Result | 95% interval |
| --- | ---: | ---: |
| Referenced-image coverage | 6,363 / 7,701 = 82.63% | Wilson 81.76%-83.46% |
| Pair coverage | 4,126 / 6,000 = 68.77% | Wilson 67.58%-69.93% |
| Accuracy at cosine threshold 0.363, scored pairs only | 4,093 / 4,126 = 99.20% | Wilson 98.88%-99.43% |
| False accept rate at 0.363 | 3 / 2,068 = 0.145% | Wilson 0.049%-0.426% |
| False reject rate at 0.363 | 30 / 2,058 = 1.458% | Wilson 1.023%-2.073% |
| ROC-AUC, scored pairs only | 0.998553 | Stratified pair bootstrap 0.997300-0.999439 |
| Correct-and-scored yield, all protocol pairs | 4,093 / 6,000 = 68.22% | Wilson 67.03%-69.38% |

The strict input policy rejected 1,338 of the 7,701 referenced images: 1,310
because more than one face was detected and 28 for insufficient sharpness. As a
result, 1,874 protocol pairs were unscored. Conditional accuracy must therefore
always be presented with coverage and the all-pairs yield.

The pooled, same-data optimum was cosine threshold `0.3359814518`, with
99.42% conditional accuracy (4,102 / 4,126). This is a deliberately labeled
post-hoc result and is optimistic. Selecting the threshold on the other nine
folds and evaluating the held-out fold produced 99.37% pooled conditional
accuracy (4,100 / 4,126), with mean fold accuracy 99.38% and sample standard
deviation 0.45 percentage points.

## Reproducibility record

| Input | SHA-256 |
| --- | --- |
| Funneled LFW archive | `b47c8422c8cded889dc5a13418c4bc2abbda121092b3533a83306f90d900100a` |
| Extracted LFW JPEG tree | `61fda5733c31c83a4c1605759851714c25ad9f1b2f1503bb044b07eb663253d7` |
| Canonical `pairs.txt` | `ea42330c62c92989f9d7c03237ed5d591365e89b3e649747777b70e692dc1592` |
| YuNet model | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| SFace model | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` |
| Benchmark script for this run | `5eafe4ae804526ab3b5ad0b51c1d15e486f0764e5296e59f4baf66d831224254` |
| Face API source for this run | `7e62360fdc695db7de0b822a5ead62683024f64d8ce7a4d9512f1790dd0bee7e` |

The FaceProof 0.2.0 release rerun reproduced every aggregate metric and input
fingerprint above. It took 243.96 seconds including pinned-input acquisition
and 172.03 seconds for the benchmark itself; face encoding took 143.48 seconds.
Runtime varies by disk, CPU, and network. The environment was Python 3.11.15
and OpenCV 4.14.0. Bootstrap results use 1,000 deterministic replicates with
seed `20260903`.

## Interpretation limits

- The fixed 0.363 threshold was previously reported for SFace on LFW, so this
  is not an independent threshold-selection experiment.
- SFace is an off-the-shelf model trained with labeled outside data. This is a
  replication, not an official LFW leaderboard submission.
- LFW is an older, curated benchmark and is not representative of current
  social-media compression, crops, occlusion, pose, or demographic mix.
- Rejected pairs are excluded from ROC-AUC, FAR, FRR, and conditional accuracy.
- The confidence intervals are descriptive. LFW pairs reuse images and are not
  strictly independent observations.

## Sources

- [LFW dataset and protocol](http://vis-www.cs.umass.edu/lfw/)
- [Huang et al., *Labeled Faces in the Wild*](https://people.cs.umass.edu/~elm/papers/Huang_eccv2008-lfw.pdf)
- [LFW updated technical report and reporting categories](https://people.cs.umass.edu/~elm/papers/lfw_update.pdf)
- [OpenCV YuNet/SFace tutorial and published operating point](https://docs.opencv.org/4.x/d0/dd4/tutorial_dnn_face.html)
- [scikit-learn LFW loader with the pinned Figshare mirror metadata](https://github.com/scikit-learn/scikit-learn/blob/main/sklearn/datasets/_lfw.py)
