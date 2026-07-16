from untaped.api import create_app

from untaped_orchestration.cli import (
    decision_commands,
    id_commands,
    maintenance_commands,
    read_commands,
    relation_commands,
    store_commands,
    task_commands,
)

app = create_app(
    name="orchestration",
    help="Coordinate typed repository tasks and decisions.",
)

id_commands.register(app)
read_commands.register(app)
task_commands.register(app)
decision_commands.register(app)
relation_commands.register(app)
store_commands.register(app)
maintenance_commands.register(app)
