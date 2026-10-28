"""Windows spawn-safe entrypoint for the independent task Worker."""
from src.jobs import main

if __name__ == '__main__':
    main()
