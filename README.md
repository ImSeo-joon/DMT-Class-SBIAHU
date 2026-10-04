# DMT Class 01 网站后台

本目录包含网站前端、Python 服务端和 SQLite 数据存储。直接双击 `index.html` 只能浏览静态原型；注册、登录、个人中心和内容编辑需要通过服务端访问。

## 本机启动

在此目录打开 PowerShell：

```powershell
python server.py create-admin
python server.py
```

首次运行 `create-admin` 时，按提示创建管理员姓名、邮箱和密码。该密码不会写入代码或配置文件。随后打开 `http://127.0.0.1:8000`。

注册需要邀请码。管理员可在「编辑后台 → 邀请码」生成，或使用命令行：

```powershell
python server.py create-invite          # 默认：班级成员身份，1 次有效
python server.py create-invite officer 5 班委
```

首次启动服务会将 `content.json` 的新闻、活动和课表导入 `var/class_site.sqlite3`。之后的后台修改保存在 SQLite 数据库中；修改 `content.json` 不会覆盖已有数据库内容。

发信账号用于把「咨询合作」和「意见反馈」转发到班委邮箱，可以试发一封确认是否可用：

```powershell
python server.py test-mail you@example.com            # 试发一封测试邮件
```

## 角色与权限

- **管理员**：管理新闻、活动、课表、公共资料、用户角色和管理记录。
- **班委、导员**：编辑新闻、活动、课表，上传或删除公共资料；不能查看或管理账号角色。
- **班级成员**：登录并查看个人中心，可浏览、下载公共资料；没有内容编辑权限。
- 注册必须填写邀请码，注册后的身份由邀请码决定（班级成员 / 班委 / 导员）。邀请码不能授予管理员，管理员只能由现有管理员在后台指派。

## 邀请码注册

注册默认采用邀请码模式，只有拿到邀请码的本班同学、班委或导员才能注册账号。管理员在「编辑后台 → 邀请码」中生成、查看和删除邀请码，可指定备注、身份、可用次数与有效天数；删除后邀请码立即失效，已注册账号不受影响。邀请码使用 `secrets` 随机生成（形如 `DMT-K7M2Q-9XPR4`），只对管理员可见。

注册方式可在后台切换：`invite`（仅邀请码，默认）、`open`（自由注册，新账号为班级成员）、`closed`（完全关闭注册）。前端会按当前模式自动显示或隐藏邀请码输入框。

服务端在注册接口里校验邀请码是否存在、是否被禁用、是否已用尽、是否过期，用尽次数在同一个事务内累加；每次注册都会在管理记录里留下 `register-invite:<备注>` 条目，便于核对是哪一批邀请码带来的账号。

## 邮件通知（发信配置）

发信通过 SMTP，配置写在服务器的 `/etc/dmt-class-site.env`（绝不要提交到仓库）。一键写入并试发：`sudo bash deploy/setup-mail.sh`；各邮箱服务商的参数对照和说明见 `deploy/dmt-class-site.env.example`；只发测试邮件用 `sudo bash deploy/setup-mail.sh --test 你的邮箱`，查看当前配置用 `--show`。

```ini
SMTP_HOST=smtp.example.com
SMTP_PORT=465
SMTP_USER=class@example.com
SMTP_PASSWORD=授权码
SMTP_SENDER=class@example.com
SMTP_SENDER_NAME=DMT CLASS 01
# 可选：ssl（默认 465）、starttls（默认 587）、none（仅本机测试）
SMTP_SECURITY=ssl
```

腾讯云、阿里云等主机默认封禁 25 端口，所以请用 465 或 587 提交端口，并使用邮件服务商或班级邮箱的授权码。自建发信需要配置 SPF/DKIM，否则容易被判为垃圾邮件；上线前先用后台的「发送测试邮件」或 `python server.py test-mail` 验证一次。

## 组队大厅

面向班级成员的组队与协作板块，入口在主导航「组队大厅」，**需要登录才能使用**（未登录只显示提示）。

发帖内容包含分类（竞赛 / 科研 / 活动 / 课程 / 其他）、标题（≤80 字）、需要什么（≤200 字）与详细说明（≤2000 字）；每条帖子下可以在线回复，形成一对多的交流串。发帖人与管理员、班委、导员可以删除帖子（回复随之级联删除），发帖人本人还可以把帖子标记为"已组满"或重新开放；所有写入都经过登录态、CSRF 令牌与来源校验，并按 IP 限流（发帖 15 分钟 10 条、回复 15 分钟 30 条）。帖子与回复分别存在 `team_posts`、`team_replies` 两张表，发帖与删除会写入管理记录。

## 简历 AI 润色（用户自带密钥）

推免指南的「自制简历」里带一个 AI 润色面板，采用"用户自带密钥"（BYOK）模式，服务器本身不需要任何模型密钥：

- 使用者在页面上填入自己的 DeepSeek API Key，密钥只保存在本人浏览器的 localStorage；
- 生成时请求经本站后端临时转发给模型服务，服务器**不落库、不写日志**（审计只记录使用的模型名）；
- 接口 `POST /api/ai/resume` 要求登录、CSRF 令牌与合法来源，并按 IP 限流（15 分钟 30 次）；
- 发送内容自动剔除"联系方式"字段，页面要求使用者勾选同意后才会发起请求；
- 生成结果只进入预览区，由使用者点「采用」才写入简历；条目数与模型输出数量不一致时拒绝写入，避免张冠李戴；
- 提示词中明确要求模型不得编造经历、数据与奖项。

模型白名单为 `deepseek-chat` 与 `deepseek-reasoner`（其他取值自动回落到前者）。如需指向自建网关或代理，配置一个可选环境变量即可：

```ini
AI_BASE_URL=https://api.deepseek.com
```

服务端会在每个写入接口检查会话、CSRF 令牌和数据库中的当前角色。密码使用 PBKDF2-HMAC-SHA256 加盐保存；会话 Cookie 设置 HttpOnly 和 SameSite。

## 更新流程

登录后打开「编辑后台」，可以发布双语新闻、活动、上传或删除公共资料并编辑周课表。公共资料模块支持班级成员浏览和下载 PDF、Office 文档、文本、ZIP 与常见图片；单个文件上限 25 MB。编辑课表时可修改学期第 1 周周一，网站会据此按北京时间重新计算当前周次。新闻图片需先放进 `assets` 目录，再在后台填写相对路径，例如 `assets/event-photo.jpg`。

公共资料支持文件夹：班委及以上可以在「公共资料」页面点「＋ 新建文件夹」，点中某个文件夹后可以重命名或删除（删除文件夹不会删资料，里面的文件会回到「未归档」）。上传时可以在表单里选择「放入文件夹」，已经上传的资料也可以在「编辑后台 → 公共资料」的列表里用下拉框随时改所属文件夹。文件夹只有一层，不做嵌套。

`content.json` 只在数据库第一次创建时用来灌入初始内容。网站已经在运行之后再往这个文件里加新闻是不生效的，线上读的一直是数据库。如果某次代码更新里带了新的新闻条目，用 `deploy/sync-news.py` 把它们补进数据库（默认只做检查，加 `--apply` 才真正写入，featured 的互斥规则和后台一致）：

```bash
sudo -u dmt-site env DMT_DB_PATH=/var/lib/dmt-class-site/class_site.sqlite3 \
  /opt/dmt-class-site/.venv/bin/python /opt/dmt-class-site/deploy/sync-news.py --apply
```

## 腾讯云 Linux 上线准备

`server.py` 保留给本地预览和 CLI 管理操作。公网服务入口使用 `app.py`（Flask WSGI）和 Waitress，由 Nginx 提供 HTTPS 反向代理。不要将 Python 开发服务器或 8000 端口直接暴露到公网。

### 代码与数据

- 上传 `app.py`、`server.py`、`requirements.txt`、`index.html`、`content.json`、`assets/`、`deploy/` 和本 README 到私有 GitHub 仓库。
- 不要上传 `var/`、`.env`、SQLite 数据库、备份、密码或服务器密钥。`.gitignore` 已加入相应排除项；网页端手动上传仍要自行跳过这些内容。
- 生产数据库建议放在 `/var/lib/dmt-class-site/class_site.sqlite3` 等独立持久目录；上传资料默认存储在同级 `uploads/`。数据库和资料文件都要定期备份并限制备份访问权限。代码更新不应覆盖这个目录。`DMT_UPLOAD_DIR` 可用于指定另一个持久目录。

### 服务器配置目标

1. 安装 Ubuntu 系统更新、Python venv、Nginx。
2. 将代码放在 `/opt/dmt-class-site`，创建独立服务账号 `dmt-site`；数据库目录设为 `/var/lib/dmt-class-site`，只允许服务账号读写。
3. 在代码目录创建虚拟环境并安装依赖：

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

4. 创建仅 root 可读的 `/etc/dmt-class-site.env`，内容按实际值填写：

   ```ini
   DMT_DB_PATH=/var/lib/dmt-class-site/class_site.sqlite3
   SITE_ORIGIN=https://你的域名
   ```

5. 将 `deploy/dmt-class-site.service.example` 复制为 systemd 服务配置，按实际安装路径确认配置后启用。它让 Waitress 只监听 `127.0.0.1:8000`；Nginx 才能从公网接收网页请求。
6. 备案获批、域名解析到服务器且 HTTPS 证书已签发后，复制并修改 `deploy/nginx-https.conf.example`，填入真实域名和证书路径。防火墙只开放 SSH 管理端口及网站所需的 80/443，不开放 8000。
7. 首次在服务器创建线上管理员（会写入生产数据库）：

   ```bash
   sudo -u dmt-site env DMT_DB_PATH=/var/lib/dmt-class-site/class_site.sqlite3 \
     /opt/dmt-class-site/.venv/bin/python /opt/dmt-class-site/server.py create-admin
   ```

重置密码使用同一个数据库路径运行 `server.py reset-password 管理员邮箱`。不要把生产数据库复制进 GitHub。

**开放前的待办**：完成备案；确认证书和 HTTPS；设置数据库备份；核对系统服务、防火墙和域名来源配置。内地服务器须在备案完成后才对公网开放网站。注册不发送验证邮件，忘记密码由服务器管理员重置。

## 本地启动

本地预览仍可在本目录运行 `python server.py`。管理员创建和密码重置命令也继续使用 `server.py`。生产环境安装 `requirements.txt` 后运行 `app.py` WSGI 应用，不运行本地预览服务器。
