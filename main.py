from amicus.server import main as server_main
import sys

if __name__ == '__main__':
    argv = sys.argv[1:]
    if not argv: exit(1)
    if argv[0] == 'server':
        print('running server')
        server_main(argv[1:], f'{sys.argv[0]} {argv[0]}')
