# DMT CLASS 01 —— 抗 DDoS 加固清单

## 先回答：改完能防 DDoS 吗

**不能"防止"，只能"扛得住小的、并且让别人打不到你的真实 IP"。**

要分清两件事：

1. **打死服务器的东西**是流量本身。阿里云 ECS 自带约 5 Gbps 的 DDoS 基础防护，超过阈值就把你的公网 IP 拉进**黑洞**——所有端口和 ping 全部无响应。你的站之前就是这么挂的，表现出来的特征很典型：80/443/22 全超时、ping 100% 丢包。这种情况 Nginx 一点忙都帮不上，因为流量根本没走到 Nginx，在阿里云的入口就被丢了。
2. **打死进程的东西**才是应用层的小流量：脚本狂刷登录接口、狂发留言、一次性并发抓首页。这些 Nginx 限流能挡住。

所以加固的价值在第二类，以及"让第一类更难发生"：把真实 IP 藏起来，攻击者只能打到 CDN/高防的 IP，而不是你的 ECS。

**能做到的：** 脚本刷接口 → 429；单手并发抓站 → 打不垮；别人查不到你源站 IP → 没法直接砸你。
**做不到的：** 真的有人用几十 G 流量砸你的域名。那必须买**DDoS 高防**，把流量在上游洗掉。

---

## 三层结构

| 层 | 干什么 | 本项目怎么做 |
| --- | --- | --- |
| 上游 | 把攻击流量吸走 | 域名 CNAME 到阿里云 CDN / DDoS 高防 |
| 源站入口 | 让人找不到真实 IP | 安全组只放行 CDN 回源 IP 段，不开放 8000 |
| 源站内部 | 扛住漏进来的流量 | Nginx 限流（本仓库配置）+ 静态文件不经过 Python |

**顺序不能反：先装限流 → 再挂 CDN → 最后才收窄安全组。** 反过来做会把自己锁在门外，或者让全班一起被限流。

---

## 第 0 步：确认现在是不是还在黑洞

在**你自己的电脑**（不是服务器）上执行：

```powershell
Test-NetConnection 8.218.98.81 -Port 443 -InformationLevel Detailed
```

- `TcpTestSucceeded : True` → 正常，可以继续往下做。
- 一直卡住最后 `False`，同时 ping 也 100% 丢包 → 大概率还在黑洞。

黑洞期间**不要反复重启服务器**，重启没有任何用，解封是阿里云自动做的，通常从 2.5 小时起、反复被攻击会逐次加长。想立刻恢复只有两条路：换一个公网 IP（控制台里换弹性公网 IP），或者等。

不管走哪条路，**先把域名挂到 CDN**，否则下次还会被同样方式打死。

---

## 第 1 步：装限流配置

仓库里已经准备好两个文件，直接复制到服务器：

```bash
# 1) http 层的限流 zone（必须先装，否则 nginx -t 会报找不到 dmt_general）
sudo cp /opt/dmt-class-site/deploy/nginx-ratelimit.conf.example /etc/nginx/conf.d/00-dmt-ratelimit.conf

# 2) 站点配置：把里面的 class.example.edu 全部替换成你的真实域名（例如 dmt01sbiahu.com）
sudo cp /opt/dmt-class-site/deploy/nginx-https.conf.example /etc/nginx/conf.d/dmt-class-site.conf
sudo nano /etc/nginx/conf.d/dmt-class-site.conf

# 3) 检查并生效
sudo nginx -t
sudo systemctl reload nginx
```

`nginx -t` 如果提示 `unknown limit_req_zone "dmt_general"`，说明第 1 个文件没放进去，或者 `nginx.conf` 里没有 `include /etc/nginx/conf.d/*.conf;`。

**这一步做完先观察一天**，看有没有误伤：

```bash
sudo grep 'limiting' /var/log/nginx/error.log | tail -20
```

有正常同学被拦（出现 429 页面），就把 `00-dmt-ratelimit.conf` 里的 `rate=30r/s` 调大到 `rate=60r/s`，或把 `burst` 加大，再 reload。

---

## 第 2 步：收窄安全组

阿里云控制台 → 云服务器 ECS → 实例 → 安全组 → 配置规则 → 入方向。

⚠️ **顺序：先把 SSH 的新端口/新来源加进去，确认能登进来，再删除旧规则。** 否则你会把自己关在服务器外面，只能走控制台 VNC 救援。

| 方向 | 协议 | 端口 | 授权对象 | 说明 |
| --- | --- | --- | --- | --- |
| 入方向 | 自定义 TCP | 443 | CDN 回源 IP 段 | HTTPS 回源，收窄后别人 ping 到也没用 |
| 入方向 | 自定义 TCP | 80 | CDN 回源 IP 段 | 跳转 + 证书续签（见第 4 步） |
| 入方向 | 自定义 TCP | 例如 51820 | 你的家庭宽带公网 IP `/32` | SSH。改成高位端口并只放行你自己的 IP |
| 入方向 | 自定义 TCP | 22 | **删除这条** | 默认的 0.0.0.0/0 SSH 是全网扫描的重灾区 |
| 出方向 | 全部 | 全部 | 0.0.0.0/0 | 保持默认不放 |

要删掉的默认规则长这样：`入方向 / 全部 / 全部 / 0.0.0.0/0`——**这条一定要删**，它意味着 8000 端口也对全网开放。

同时确认这两件事：

- `ExecStart` 里的 Waitress 只监听 `127.0.0.1:8000`（`deploy/dmt-class-site.service.example` 已经是这样），所以 8000 不需要、也不应该出现在安全组里。
- 在服务器上执行 `sudo ss -lntp`，8000 那一行应该显示 `127.0.0.1:8000` 而不是 `0.0.0.0:8000`。如果是后者，说明 systemd 单元被改过，要改回去。

顺手把 SSH 密码登录关掉，改用密钥：

```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

**执行前务必确认你已经能用密钥登录，并且开着一个没断的 SSH 会话**，不然这条命令一样会把你锁在外面。

---

## 第 3 步：域名挂 CDN，把自己的 IP 藏起来

前置条件：域名已完成备案（内地服务器必须），`dmt01sbiahu.com` 能正常解析。

1. 阿里云控制台 → CDN → 域名管理 → 添加域名。
2. 加速域名填 `dmt01sbiahu.com`，源站类型选「IP」，源站地址填 `8.218.98.81`，端口 `443`，勾选「HTTPS 回源」。
3. 按提示去 DNS 控制台把域名的 `A` 记录改成 CDN 给的 `CNAME` 记录（**这一步做完，域名就不再直接指向你的 ECS 了**）。
4. 在 CDN 控制台申请/上传证书，开启「强制 HTTPS 跳转」。
5. CDN → 回源配置 → 找到**回源 IP 地址段**，抄下来。这就是第 2 步安全组里「授权对象」要填的东西。
6. 打开 `00-dmt-ratelimit.conf`，把最下面 `set_real_ip_from` 那几行的注释去掉，填上第 5 步抄到的 IP 段，reload。

第 6 步不做的话，Nginx 看到的全是 CDN 节点 IP，**全班同学会被当成同一个人**一起限流。

**必须实测一次**（用手机移动网络访问网站，不要用家里 Wi-Fi）：

```bash
sudo tail -f /var/log/nginx/access.log
```

看刷出来的是不是你手机的 IP（浏览器搜“IP”对照）。是 CDN 节点 IP 就说明没配对，先把 `set_real_ip_from` 注释回去，再查回源配置里的「回源请求头 / 真实 IP」设置。

安全组要**等这一步完全验证通过之后再收窄**。

---

## 第 4 步：证书续签有个坑

安全组把 80 端口收窄到 CDN IP 之后，Let's Encrypt 的 HTTP-01 校验会失败（它从公网随机 IP 来访问 `/.well-known/acme-challenge/`），证书到期就续不上。

三个可选做法，任选一个：

1. 用阿里云的免费 SSL 证书（控制台 → 数字证书管理服务），在 CDN 上部署，源站用自签或 CDN 的证书——**最省事，推荐**。
2. 改用 DNS-01 校验：`certbot` 加阿里云 DNS 插件，不需要 80 端口。
3. 到期前临时把 80 端口放回 `0.0.0.0/0` 几分钟，续完再收窄——麻烦但可行。

---

## 验证清单

一条条确认，全过才算加固完成：

| 检查 | 命令 / 做法 | 期望结果 |
| --- | --- | --- |
| 从外网 ping 不到源站 | 别的网络 `ping 8.218.98.81` | 超时或不可达（说明安全组生效） |
| 8000 不对外 | `Test-NetConnection 8.218.98.81 -Port 8000` | False |
| 网站正常 | 浏览器打开域名 | 页面正常、登录正常 |
| 首页有压缩 | `curl -sI -H 'Accept-Encoding: gzip' https://你的域名/` | 有 `content-encoding: gzip` |
| 静态文件不过 Python | 打开一张图，`sudo journalctl -u dmt-class-site --since "1 min ago"` | 没有对应的请求日志 |
| 真实 IP 正确 | 手机流量访问 + `sudo tail /var/log/nginx/access.log` | 显示手机公网 IP |
| 限流生效 | 本地 `for i in $(seq 1 60); do curl -so /dev/null -w '%{http_code} ' https://你的域名/api/login; done` | 出现 `429` |
| 没误伤 | `sudo grep limiting /var/log/nginx/error.log` | 正常时段几乎没有 |

---

## 万一又被打了

1. 先在阿里云控制台 → 云监控，看 ECS 的**网络流入带宽**曲线。它在被打时会顶到很高然后掉零（黑洞）。
2. 别重启服务器，没用。
3. 如果域名已经挂了 CDN，源站被黑洞不影响 CDN 缓存命中的请求，同学还能看到部分内容；把 CDN 缓存时间调长（静态资源从 1 小时调到 7 天）能多扛一会。
4. 想彻底解决只有升**DDoS 高防**（付费，最便宜的保底带宽版本也比 CDN 贵）。

---

## 和这次改动无关但值得顺手做的一件事

阿里云控制台 → 云监控 → 创建报警规则，对 ECS 的「网络流入带宽」和「CPU 使用率」设阈值报警（比如带宽 > 80 Mbps 就发短信）。被打的时候你会第一时间收到通知，而不是等同学问“网站怎么打不开了”。
