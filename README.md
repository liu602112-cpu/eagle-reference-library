# Eagle 视觉参考检索库

给 Eagle 图库建立本地索引，按分类、标签和配色寻找视觉参考。

源 Eagle 图库只读；索引、视觉注释和网页在独立目录保存。

```text
python scripts/eagle_library.py --library <你的Eagle.library> --data-root ./data sync
python scripts/eagle_library.py --library <你的Eagle.library> --data-root ./data search --query "封面 蓝色"
python scripts/eagle_library.py --library <你的Eagle.library> --data-root ./data gallery
```

Python 3.11 或更新版本，无第三方 Python 依赖。在线画廊使用本仓库原创示例，个人原图、绝对路径和数据库不随包发布。
