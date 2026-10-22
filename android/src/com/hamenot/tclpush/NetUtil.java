package com.hamenot.tclpush;

import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.List;

/** 网络小工具：挑本机内网地址、探端口、校验 IP 写法。 */
public final class NetUtil {

    private NetUtil() {
    }

    /**
     * 取本机局域网地址。
     *
     * 手机同样有多网卡问题：VPN、虚拟网卡、蜂窝数据都可能冒出来。打分规则和
     * Windows 版一致 —— 192.168 最高，10./172.16-31 次之，198.18/19 这类
     * 代理段和 169.254 链路本地地址直接排除。
     */
    public static String lanIp() {
        List<String> cands = new ArrayList<String>();

        // 默认路由会用哪个地址出门，通常就是 Wi-Fi 地址
        DatagramSocket ds = null;
        try {
            ds = new DatagramSocket();
            ds.connect(InetAddress.getByName("8.8.8.8"), 53);
            InetAddress a = ds.getLocalAddress();
            if (a != null) {
                cands.add(a.getHostAddress());
            }
        } catch (Exception ignored) {
        } finally {
            if (ds != null) {
                ds.close();
            }
        }

        try {
            Enumeration<NetworkInterface> nis = NetworkInterface.getNetworkInterfaces();
            while (nis != null && nis.hasMoreElements()) {
                NetworkInterface ni = nis.nextElement();
                try {
                    if (!ni.isUp() || ni.isLoopback()) {
                        continue;
                    }
                } catch (Exception e) {
                    continue;
                }
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    InetAddress a = ia.getAddress();
                    if (a instanceof Inet4Address && !a.isLoopbackAddress()) {
                        cands.add(a.getHostAddress());
                    }
                }
            }
        } catch (Exception ignored) {
        }

        String best = "127.0.0.1";
        int bestScore = Integer.MIN_VALUE;
        for (String ip : cands) {
            int s = score(ip);
            if (s > bestScore) {
                bestScore = s;
                best = ip;
            }
        }
        return best;
    }

    static int score(String ip) {
        if (ip == null) {
            return -99;
        }
        if (ip.startsWith("127.") || ip.startsWith("169.254.")) {
            return -1;
        }
        if (ip.startsWith("198.18.") || ip.startsWith("198.19.")) {
            return 0;
        }
        if (ip.startsWith("192.168.")) {
            return 4;
        }
        if (ip.startsWith("10.")) {
            return 3;
        }
        if (ip.matches("172\\.(1[6-9]|2\\d|3[01])\\..*")) {
            return 3;
        }
        if (ip.contains(":")) {
            return -2;      // IPv6 先不用
        }
        return 1;
    }

    public static boolean validIp(String ip) {
        if (ip == null) {
            return false;
        }
        String s = ip.trim();
        String[] parts = s.split("\\.");
        if (parts.length != 4) {
            return false;
        }
        for (String p : parts) {
            if (p.isEmpty() || p.length() > 3) {
                return false;
            }
            try {
                int v = Integer.parseInt(p);
                if (v < 0 || v > 255) {
                    return false;
                }
            } catch (NumberFormatException e) {
                return false;
            }
        }
        return true;
    }

    /** 端口是否可连上，用来做「测试连接」。 */
    public static boolean portOpen(String ip, int port, int timeoutMs) {
        Socket s = new Socket();
        try {
            s.connect(new InetSocketAddress(ip, port), timeoutMs);
            return true;
        } catch (Exception e) {
            return false;
        } finally {
            try {
                s.close();
            } catch (Exception ignored) {
            }
        }
    }
}
