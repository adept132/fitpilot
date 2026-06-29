import enum

class ExerciseAction(str, enum.Enum):
    push = "push"
    pull = "pull"
    squat = "squat"
    hinge = "hinge"
    flexion = "flexion"
    extension = "extension"
    abduction = "abduction"
    adduction = "adduction"
    core = "core"
    elevation = "elevation"  # Новое
    plantarflexion = "plantarflexion"  # Новое
    rotation = "rotation"  # Новое
    shoulder_extension = "shoulder_extension"  # Новое
    lateral_flexion = "lateral_flexion"  # Новое
    carry = "carry"  # Новое
    unknown = "unknown"

class ExerciseVector(str, enum.Enum):
    vertical = "vertical"
    horizontal = "horizontal"
    incline = "incline"
    decline = "decline"
    unknown = "unknown"

class ExerciseLaterality(str, enum.Enum):
    bilateral = "bilateral"
    unilateral = "unilateral"
    unknown = "unknown"