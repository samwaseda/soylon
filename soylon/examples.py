import flowrep as fr


def get_speed(distance: float, time: float) -> float:
    speed = distance / time
    return speed


def get_kinetic_energy(mass: float, velocity: float) -> float:
    return 0.5 * mass * velocity**2


@fr.workflow
def my_workflow(distance: float, time: float, mass: float) -> float:
    speed = get_speed(distance, time)
    kinetic_energy = get_kinetic_energy(mass, speed)
    return kinetic_energy
