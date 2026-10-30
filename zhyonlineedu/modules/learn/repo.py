"""learn 模块仓储层（§4.1）。

红线：本模块仅允许被 workers/progress_flush.py（单写者）调用；
禁止被 api/、services/ 层 import（CI import 路径扫描 + 字符串扫描双检）。
learn_progress / learn_course_progress 的所有写入走 GREATEST 幂等语句。
"""
