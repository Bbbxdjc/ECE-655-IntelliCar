import time

print("Starting wall-clock drift test...")
t0_wall = time.time()
t0_perf = time.perf_counter()

while True:
    time.sleep(1)
    t_wall = time.time()
    t_perf = time.perf_counter()

    wall_elapsed = t_wall - t0_wall
    perf_elapsed = t_perf - t0_perf

    print(f"Wall clock elapsed: {wall_elapsed:.2f} s, "
          f"Perf elapsed: {perf_elapsed:.2f} s, "
          f"Drift = {perf_elapsed - wall_elapsed:.2f} s")
