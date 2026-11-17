"""隔离模式测试入口：显式登记项目路径，不依赖 cwd 或环境变量。

运行：python3 -I tests/run_tests.py
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if __name__ == '__main__':
    suite = unittest.TestLoader().discover(os.path.join(ROOT, 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(1 if result.failures or result.errors else 0)
