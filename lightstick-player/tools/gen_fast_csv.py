import csv
cols = ["frame_time_ms", "frame_id", "frame_type", "marker"]
for i in range(9):
    cols += [f"ch{i}_function", f"ch{i}_red", f"ch{i}_green", f"ch{i}_blue"]
palette = [(15,0,0),(0,15,0),(0,0,15),(15,15,0),(0,15,15),(15,0,15),(15,15,15),(0,0,0)]
rows = []
for k in range(40):
    r, g, b = palette[k % len(palette)]
    row = [k * 60, k, "color" if (r, g, b) != (0, 0, 0) else "blackout", ""]
    for i in range(9):
        row += [0, r, g, b]
    rows.append(row)
with open("examples/fast60.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(cols)
    w.writerows(rows)
print("wrote examples/fast60.csv", len(rows), "frames @60ms")