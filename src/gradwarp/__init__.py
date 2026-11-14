"""Empirical gradient nonlinearity field recovery (openspec add-fasttrack-gradwarp-recovery).

`coords` maps a per-session registration into scanner coordinates; `shfield` fits one
spherical-harmonic field across sessions. Neither module touches S3, NBDC or any file
holding participant data; they work on arrays and on ANTs transform files.
"""
