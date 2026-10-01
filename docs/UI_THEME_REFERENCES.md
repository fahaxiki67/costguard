# 工作台主题参考记录（v0.1.32）

本轮采用现有 PySide6 / Qt Fusion / QPalette / QSS 实现，保持暖炭黑与红橙配色。
未引入新的主题运行库。应用内界面采用固定主题；系统文件选择器沿用系统设置。

## 已采用的方法

- PyQtDarkTheme：QPalette 与 QSS 配合，区分背景层、输入框、选中态、悬停态、
  滚动条轨道与滑块。将同一套状态处理方式落实到价盾现有全局主题。
- QDarkStyleSheet：补齐水平/垂直滚动条的一致状态、禁用输入与弹窗按钮的尺寸约束。
- 总览以中间色面板承载九张指标卡片，卡片沿用现有布局和系统字体；业务数据与
  状态文字不变。当前选中标签使用暖红底与珊瑚橙下划线。

## 来源快照

- [PyQtDarkTheme](https://github.com/5yutan5/PyQtDarkTheme)，MIT；
  核对提交 `96eb7e368039ab446952573bafc17d54b7ef5123`，
  `style/base.qss`、`style/colors/themes/dark.json` 与 palette 用法。
- [QDarkStyleSheet](https://github.com/ColinDuquesnoy/QDarkStyleSheet)，代码 MIT；
  核对提交 `6f3962e63749893f951be73ebfec93e83fffebf2`，
  `qdarkstyle/dark/darkstyle.qss` 与 `LICENSE.rst`。
- [PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets) 已查看，
  本轮未复制其代码或加入依赖。

配色与卡片 QSS 按价盾现有 token 独立编写；滚动条轨道及状态规则参考上述项目。
未复制第三方图标或图片。下方保留被参考样式代码的 MIT 通知。

## MIT 通知

Copyright (c) 2021-2022 Yunosuke Ohsugi
Copyright (c) 2013-2019 Colin Duquesnoy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
