# heavy_math.py (เวอร์ชันแก้ไขวิธี A ให้วัด RAM ลึกถึงวัตถุข้างใน)
import math
import random
import sys

def get_deep_size(lst):
    # 1. วัดขนาดของโครงสร้างกล่อง List เปล่าๆ (จะได้ประมาณ 10.50 MB)
    total_size = sys.getsizeof(lst)
    
    # 2. ถ้ามีข้อมูลในกล่อง ให้บวกขนาดของวัตถุ Float ทุกตัวเข้าไปด้วย (ตัวละประมาณ 24 Bytes)
    if len(lst) > 0:
        total_size += sum(sys.getsizeof(item) for item in lst)
        
    return total_size

def simulate_workload():
    allocated_memory = []
    
    # วนลูปสร้างข้อมูลขนาดใหญ่ยัดลง RAM (8 ล้านตัว)
    for _ in range(8000000):
        allocated_memory.append(random.random())
        
    # รันคำนวณคณิตศาสตร์เพื่อดึงเวลา Latency
    for i in range(1000000):
        val = math.sin(i) * math.cos(i)

    # แก้ไขวิธี A: เรียกใช้ฟังก์ชันคำนวณแบบลึก (Deep Size) เพื่อให้รวมวัตถุข้างใน
    true_ram_bytes = get_deep_size(allocated_memory)
    true_ram_mb = true_ram_bytes / (1024 * 1024)
    
    print(f"True Allocation Size (Deep): {true_ram_mb:.2f} MB")

if __name__ == "__main__":
    simulate_workload()