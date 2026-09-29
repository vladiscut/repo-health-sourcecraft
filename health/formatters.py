def format_rating(num) -> str:
    # Обрабатываем отрицательные числа
    is_negative = num < 0
    num = abs(num)

    # Меньше 1000 возвращаем как есть (без дробной части, если это целое)
    if num < 1000:
        result = str(int(num) if num.is_integer() else num)
    else:
        # Список суффиксов для тысяч, миллионов, миллиардов и триллионов
        suffixes = ['', 'K', 'M', 'B', 'T']

        # Находим индекс суффикса (сколько раз число делится на 1000)
        power = 0
        while num >= 1000 and power < len(suffixes) - 1:
            num /= 1000.0
            power += 1

        # Округляем до 1 знака после запятой
        # Если после округления получилась .0 (например, 2.0), убираем её
        val = round(num, 1)
        if val.is_integer():
            result = f"{int(val)}{suffixes[power]}"
        else:
            result = f"{val}{suffixes[power]}"

    return f"-{result}" if is_negative else result


def format_percentile(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    if number < 1:
        shown = f"{number:.1f}".rstrip("0").rstrip(".")
    else:
        shown = str(int(round(number)))
    return f"TOP {shown}%"


def humanize_number(num) -> str:
    # Заменяем подчеркивание (дефолт для Python) на пробел
    return f"{num:_}".replace("_", " ")
