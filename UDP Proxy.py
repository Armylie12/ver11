import socket
import threading

LOCAL_IP = "0.0.0.0"
LOCAL_PORT = 12345  # พอร์ตที่ EmotiBit ส่งข้อมูลมา
FORWARD_IP = "127.0.0.1"
FORWARD_PORT = 12346  # พอร์ตที่ OSC Server จะฟัง

def proxy():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((LOCAL_IP, LOCAL_PORT))
    print(f"UDP Proxy listening on {LOCAL_IP}:{LOCAL_PORT}")

    forward_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    while True:
        data, addr = sock.recvfrom(1024)
        forward_sock.sendto(data, (FORWARD_IP, FORWARD_PORT))

if __name__ == "__main__":
    threading.Thread(target=proxy, daemon=True).start()
