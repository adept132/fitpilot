import enum


class DayTemplateType(str, enum.Enum):
    PUSH = "push"
    PULL = "pull"
    LEGS = "legs"
    UPPER = "upper"
    LOWER = "lower"
    ARMS_SHOULDERS = "arms_shoulders"
    FULL_BODY = "full_body"
    ACTIVE_REST = "active_rest"
    CUSTOM = "custom"