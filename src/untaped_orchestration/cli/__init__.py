from untaped.api import create_app

app = create_app(
    name="orchestration",
    help="Coordinate typed repository tasks and decisions.",
)
