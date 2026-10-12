// tsnet-socks：把 tailnet 节点嵌进单个 exe，面向考生机的 P2P 上传通道。
// 免管理员、免 TUN 驱动、免命名管道（这些是 stock tailscaled 在 Windows 上的坑）。
//
// 用法：
//   tsnet-socks -control-url https://headscale.alethicinsight.com \
//       -auth-key hskey-auth-xxx -hostname candidate-08260000 \
//       -state-dir ts_state -socks5 127.0.0.1:1055
//
// 入网成功后 stdout 输出一行 "READY <tailnet-ip>"，失败输出 "FAIL ..."。
// 收到 SIGINT/SIGTERM 时优雅注销（节点为 ephemeral，服务端 30min 无活动自动蒸发）。
package main

import (
	"context"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	socks5 "github.com/armon/go-socks5"
	"tailscale.com/tsnet"
)

func main() {
	var (
		controlURL = flag.String("control-url", "", "headscale 控制面地址")
		authKey    = flag.String("auth-key", "", "预授权密钥")
		hostname   = flag.String("hostname", "candidate", "节点名")
		stateDir   = flag.String("state-dir", "ts_state", "状态目录")
		socksAddr  = flag.String("socks5", "127.0.0.1:1055", "SOCKS5 监听地址")
		timeout    = flag.Duration("timeout", 20*time.Second, "入网超时")
	)
	flag.Parse()
	if *controlURL == "" || *authKey == "" {
		fmt.Println("FAIL control-url and auth-key are required")
		os.Exit(2)
	}
	logf := func(format string, args ...any) {
		fmt.Fprintf(os.Stderr, "[tsnet] "+format+"\n", args...)
	}

	srv := &tsnet.Server{
		ControlURL: *controlURL,
		Hostname:   *hostname,
		AuthKey:    *authKey,
		Dir:        *stateDir,
		Ephemeral:  true,
		Logf:       logf,
		UserLogf:   logf,
	}
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	st, err := srv.Up(ctx)
	if err != nil {
		fmt.Printf("FAIL up: %v\n", err)
		os.Exit(1)
	}
	ip := ""
	if len(st.TailscaleIPs) > 0 {
		ip = st.TailscaleIPs[0].String()
	}

	s5, err := socks5.New(&socks5.Config{
		Dial: func(ctx context.Context, network, addr string) (net.Conn, error) {
			if !strings.HasPrefix(network, "tcp") {
				return nil, fmt.Errorf("only tcp supported")
			}
			return srv.Dial(ctx, network, addr)
		},
	})
	if err != nil {
		fmt.Printf("FAIL socks5 init: %v\n", err)
		os.Exit(1)
	}
	ln, err := net.Listen("tcp", *socksAddr)
	if err != nil {
		fmt.Printf("FAIL listen %s: %v\n", *socksAddr, err)
		os.Exit(1)
	}

	sig := make(chan os.Signal, 2)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sig
		srv.Close()
		os.Exit(0)
	}()

	fmt.Printf("READY %s\n", ip)
	os.Stdout.Sync()
	if err := s5.Serve(ln); err != nil {
		fmt.Printf("FAIL serve: %v\n", err)
		os.Exit(1)
	}
}
