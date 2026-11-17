import flowrep as fr
from pathlib import Path


def get_speed(distance: float, time: float) -> float:
    speed = distance / time
    return speed


def get_kinetic_energy(mass: float, velocity: float) -> float:
    return 0.5 * mass * velocity**2


@fr.workflow
def kinetic_energy_workflow(distance: float, time: float, mass: float) -> float:
    speed = get_speed(distance, time)
    kinetic_energy = get_kinetic_energy(mass, speed)
    return kinetic_energy


def plot(file_path: Path):
    import matplotlib.pylab as plt

    plt.figure(figsize=(10, 6))
    plt.title("Kinetic Energy vs Distance")
    plt.xlabel("Distance (m)")
    plt.ylabel("Kinetic Energy (J)")
    plt.grid()
    with open(file_path, "r") as f:
        for line in f:
            distance, kinetic_energy = map(float, line.strip().split(","))
            plt.scatter(distance, kinetic_energy, color="blue")
    return plt


@fr.workflow
def kinetic_energy_plot_workflow(file_path: Path):
    plt = plot(file_path)
    return plt
