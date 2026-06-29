import re
from datetime import datetime
from dateutil.relativedelta import relativedelta


def parse_experience(text: str) -> tuple[int, str]:
    text = text.lower().strip()
    total_months = 0

    if 'полгода' in text or 'полугод' in text:
        return 6, get_level(6)

    date_match = re.search(r'\d{4}-\d{2}-\d{2}', text)
    if date_match:
        try:
            start_date = datetime.strptime(date_match.group(), '%Y-%m-%d')
            delta = relativedelta(datetime.now(), start_date)
            total_months = delta.years * 12 + delta.months
            return int(total_months), get_level(total_months)
        except:
            pass

    matches = re.findall(r'(\d+(?:\.\d+)?)\s*([летгодаггмесмесяцмполгода]+)?', text, re.IGNORECASE)

    for num_str, unit_str in matches:
        num = float(num_str)
        unit = unit_str.lower() if unit_str else ''

        if any(u in unit for u in ['лет', 'год', 'года', 'г', 'гг']):
            total_months += num * 12
        elif any(u in unit for u in ['мес', 'месяц', 'месяца', 'месяцев', 'м']):
            total_months += num * 1
        elif 'полгода' in unit:
            total_months += 6
        else:
            total_months += num

    return int(total_months), get_level(total_months)


def get_level(months: int) -> str:
    if months < 24:
        return 'beginner'
    elif months < 48:
        return 'intermediate'
    return 'advanced'


