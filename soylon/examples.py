from pathlib import Path

import flowrep as fr


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
    import numpy as np

    plt.grid()
    x, y = np.loadtxt(file_path)
    plt.plot(x, y)
    return plt


@fr.workflow
def plot_workflow(file_path: Path):
    plt = plot(file_path)
    return plt


def analyze_particles(image_path: Path) -> dict:
    import numpy as np
    from skimage import color, filters, io, measure

    image = io.imread(image_path)

    if image.ndim == 3:
        image = color.rgb2gray(image)

    threshold = filters.threshold_otsu(image)
    binary = image > threshold

    labels = measure.label(binary)
    regions = measure.regionprops(labels)

    diameters = [r.equivalent_diameter_area for r in regions if r.area >= 20]

    return {
        "particle_count": len(diameters),
        "mean_diameter_px": (float(np.mean(diameters)) if diameters.size > 0 else None),
    }


@fr.workflow
def analyze_particles_workflow(image_path: Path) -> dict:
    results = analyze_particles(image_path)
    return results
