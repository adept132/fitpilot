"""
Финальный исправленный код для извлечения команд в AIogram 3.x.
На основе логов видно, что хандлеры хранятся в observers, поэтому пробиваемся глубже.
"""

from aiogram import Router
from aiogram.filters import Command


def extract_commands_from_router(router: Router) -> list[str]:
    """Извлечение команд из observers в AIogram 3.x."""
    commands = []

    print(f"ОТЛАДКА: Сканируем роутер: {router}")

    # Проверяем observers
    if hasattr(router, 'observers') and router.observers:
        print(f"ОТЛАДКА: Роутер имеет {len(router.observers)} observers")

        for event_type, observer in router.observers.items():
            print(f"ОТЛАДКА: Observer: {event_type}")

            # Проверяем наличие handlers в observer
            if hasattr(observer, 'handlers') and observer.handlers:
                print(f"ОТЛАДКА: Observer '{event_type}' имеет {len(observer.handlers)} handlers")

                for handler in observer.handlers:
                    print(f"ОТЛАДКА: Проверяем handler: {handler}")

                    # Получаем фильтры
                    filters = []
                    if hasattr(handler, 'filters') and handler.filters:
                        filters = handler.filters
                    elif hasattr(handler, 'trigger_filters') and handler.trigger_filters:
                        filters = handler.trigger_filters

                    print(f"ОТЛАДКА: Handler имеет фильтры: {filters}")

                    for filter_ in filters:
                        if isinstance(filter_, Command):
                            print(f"ОТЛАДКА: Найден Command filter: {filter_}")

                            # Проверяем разные способы хранения команд
                            if hasattr(filter_, 'commands') and filter_.commands:
                                commands.extend(filter_.commands)
                                print(f"ОТЛАДКА: Команды из .commands: {filter_.commands}")
                            elif hasattr(filter_, '_commands') and filter_._commands:
                                commands.extend(filter_._commands)
                                print(f"ОТЛАДКА: Команды из ._commands: {filter_._commands}")
                            else:
                                # Попробуем другие атрибуты
                                print(f"ОТЛАДКА: Аттрибуты фильтра: {dir(filter_)}")
                                for attr in ['_commands', 'commands', '__dict__']:
                                    if hasattr(filter_, attr):
                                        val = getattr(filter_, attr)
                                        if val and isinstance(val, list) and len(val) > 0 and isinstance(val[0], str):
                                            commands.extend(val)
                                            print(f"ОТЛАДКА: Команды из {attr}: {val}")
                                            break
            else:
                print(f"ОТЛАДКА: Observer '{event_type}' не имеет handlers")
    else:
        print("ОТЛАДКА: Роутер не имеет observers")

    # Рекурсивно для sub-routers
    if hasattr(router, '_sub_routers') and router._sub_routers:
        for sub_router in router._sub_routers:
            commands.extend(extract_commands_from_router(sub_router))

    commands = list(set(commands))
    print(f"ОТЛАДКА: Извлечено команд: {len(commands)} {commands}")
    return commands


def get_all_commands(routers: list[Router]) -> list[str]:
    """Основная функция получения всех команд."""
    all_commands = []
    for router in routers:
        all_commands.extend(extract_commands_from_router(router))

    all_commands = list(set(all_commands))
    return sorted(all_commands)


def print_commands_list(routers: list[Router]):
    """Функция печати списка команд."""
    commands = get_all_commands(routers)

    if not commands:
        print("❌ Команды не найдены. Проверь структуру роутера вручную.")
        return

    print("✅ Найденные команды:")
    for cmd in commands:
        print(f"/{cmd}")


if __name__ == "__main__":
    print("🔍 Ищем команды...")

    try:
        from handlers.start import router as start_router
        from handlers.menu_training import router as menu_router
        from handlers.profile import router as profile_router
        from handlers.split_management import router as split_router
        from handlers.plan_workout import router as plan_workout_router
        from handlers.free_workout import router as free_workout_router
        from handlers.plan_creation import router as plan_creation_router
        from handlers.sets_input import router as sets_input_router
        from handlers.plan_execution import router as plan_execution_router
        from handlers.training_generator import router as generation_router
        from handlers.debug_handlers import router as debug_router
        from handlers.quick_log_training import router as quick_log_training_router
        from handlers.my_content import router as my_content_router
        from handlers.schedule import router as schedule_router
        from handlers.settings import router as settings_router
        from handlers.feedback import router as feedback_router

        # Список всех роутеров бота
        all_routers = [
            start_router,
            menu_router,
            profile_router,
            split_router,
            plan_workout_router,
            free_workout_router,
            plan_creation_router,
            sets_input_router,
            plan_execution_router,
            generation_router,
            debug_router,
            quick_log_training_router,
            my_content_router,
            schedule_router,
            settings_router,
            feedback_router
        ]

        # Сначала проверим один роутер для отладки
        print("Проверяем feedback роутер подробно:")
        extract_commands_from_router(feedback_router)

        print("Теперь все роутеры:")  # Добавь сюда другие роутеры

        if all_routers:
            print_commands_list(all_routers)

        # Сохраняем если есть
        commands = get_all_commands(all_routers) if 'documents' in locals() else []
        if commands:
            import json

            with open('commands_list.json', 'w', encoding='utf-8') as f:
                json.dump(commands, f, indent=2, ensure_ascii=False)
            print("Список сохранён.")
        else:
            print("Нет команд для сохранения.")

    except ImportError as e:
        print(f"❌ Ошибка импорта: {e}")

    print("Конец.")