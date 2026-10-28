# A → B 每日校时

A 每天北京时间零点通过 SSH 测量 B 的时间，将最佳样本提交给 B 的 chrony。
A 脚本仅调用 time.time/time.monotonic 读取时间，无修改系统时间的代码。A 的 systemd 服务进一步移除所有 Linux capabilities，开启 ProtectClock。
本目录独立于 Benchmark；不触发回放，不查询 Prometheus。Python 3.10 标准库即可。

## B 上准备（管理员执行）

1. 安装并运行 chrony，确认 `command -v chronyc` 和 Unix socket 的实际路径。
   常见 socket 是 `/run/chrony/chronyd.sock`，以本机为准。
2. 在现有 chrony 配置中添加 `manual`。检查已有时间源、`makestep`、`initstepslew` 等策略：
   确保日常人工输入不会触发时间跳变，且没有另一个时间服务争抢时钟。
   配置位置和服务名依发行版可能是 `/etc/chrony.conf`、`/etc/chrony/chrony.conf`，`chronyd` 或 `chrony`。
   修改配置后由管理员按维护流程重启。工具不自动修改 chrony 配置。
3. 将 receive_on_b.py 放在 `/opt/traffic-time-sync/`，目录和脚本必须由 root 拥有，普通校时账号不可写。
4. 建立专用 SSH 账号 `timesync-b`，只允许 A 的专用公钥。以 sudoers 的固定命令授权为例：

   ```sudoers
   timesync-b ALL=(root) NOPASSWD: /usr/bin/python3 /opt/traffic-time-sync/receive_on_b.py --chronyc /usr/bin/chronyc --socket /run/chrony/chronyd.sock
   ```

   用 `visudo` 配置；实际路径不同需同步替换全部配置。不要授权任意 Python 或任意 shell。
   在该公钥的 authorized_keys 行前加 `restrict,command="sudo -n /usr/bin/python3 /opt/traffic-time-sync/receive_on_b.py --chronyc /usr/bin/chronyc --socket /run/chrony/chronyd.sock"`，可额外使用 `from="A的IP"` 限定来源。
   强制命令确保该公钥只能使用校时协议。禁止为此给 Benchmark 提权。

## A 上准备

建立无管理员权限的专用账号 `timesync-a`；为它配置到 B 的 SSH 公钥认证和可信 known_hosts。
在该账号的 `~/.ssh/config` 配置别名（SSH 主机密钥须核对，不关闭校验）：

```sshconfig
Host timesync-b
    HostName B的IP
    User timesync-b
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
```

把 sync_from_a.py 放到 `/opt/traffic-time-sync/`。先以 timesync-a 身份运行只测量命令：

```bash
python3 /opt/traffic-time-sync/sync_from_a.py --host timesync-b --remote-command 'sudo -n /usr/bin/python3 /opt/traffic-time-sync/receive_on_b.py --chronyc /usr/bin/chronyc --socket /run/chrony/chronyd.sock'
```

没有 `--apply` 时，两台机器均不会收到本工具发出的校时命令。
`offset` 为 B 减 A，正数表示 B 快；`rtt` 是扣除 B 处理耗时后的网络往返秒数。
确认后增加 `--apply`，由 B 调用本机 chronyc。返回 submitted_to_chrony 仅表示 chrony 接受参考时间，不表示已经完成渐进校正。

## A 每日北京时间零点运行

把两个 time-sync-a.* 文件放到 A 的 `/etc/systemd/system/`，核对 Python、脚本、chronyc、socket 和 SSH 账号路径，然后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now time-sync-a.timer
systemctl list-timers time-sync-a.timer
journalctl -u time-sync-a.service -n 100 --no-pager
```

定时器明确采用 Asia/Shanghai，不依赖 A 的系统时区。Persistent=false 表示错过零点不在开机时补跑。
暂停自动校时：在 A 执行 `sudo systemctl disable --now time-sync-a.timer`。
确认 A 的 systemd 支持所用保护选项；不支持时应按本机版本调整部署策略。

## 行为和精度

- 每轮 3 次测量，选择最低网络 RTT 样本；SSH 建连、认证时间不计入样本。
- RTT 超过 1 秒、样本超过 30 秒、测量期间系统时钟跳变时拒绝校准。
- B 默认拒绝超过 300 秒的偏差。初始大偏差由管理员维护窗口处理，或明确调整 B 固定命令的 max-offset 参数和授权。
- B 通过 chrony 渐进调整，不运行 date/timedatectl/makestep。渐进策略仍须由管理员核对现有 chrony 配置。
- chronyc settime 仅支持整秒输入，本方案适用于秒级对齐，不承诺毫秒精度。
- A 不做校时；A 的时钟若不符合 UTC，B 只是跟随 A。
- 每天一次之间仍有漂移；根据日志监测到的偏差决定是否缩短周期。后续重新运行只测量命令可检查剩余偏差。
- 时间跳变前的已有 Prometheus 数据不会被追溯重写。

官方参考：https://chrony-project.org/doc/4.2/chronyc.html （Manual time input）。
