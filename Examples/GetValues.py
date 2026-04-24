from PyOptik.refractiveIndex import Material

bk7 = Material(r"specs/schott/optical/P-BK7.yml")

print(f"P-BK7 at 625nm: {bk7.getRefractiveIndex(625):.5f}")
print(f"P-BK7 at 587.6nm: {bk7.getRefractiveIndex(587.6):.5f}")

fused_silica = Material(r"main/SiO2/nk/Malitson.yml")

print(f"Fused Silica at 625nm: {fused_silica.getRefractiveIndex(625):.5f}")
print(f"Fused Silica at 587.6nm: {fused_silica.getRefractiveIndex(587.6):.5f}")
