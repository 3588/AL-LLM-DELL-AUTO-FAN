import subprocess
import requests # 用于从OpenHardwareMonitor获取数据
import time     # 用于实现循环和延时
import re       # 保留，以备将来解析需要

# --- 配置信息 ---
# OpenHardwareMonitor 配置
OHM_URL = "http://127.0.0.1:8085/data.json" # OpenHardwareMonitor Web服务器的URL
SCRIPT_LOOP_INTERVAL_SECONDS = 5           # 检测间隔

# iDRAC 配置信息
IDRAC_IP = "10.10.10.194"
IDRAC_USER = "root"
IDRAC_PASSWORD = "calvin" # 请注意密码安全，考虑使用更安全的方式传递密码
IPMITOOL_PATH = r"C:\DFan\ipmitool.exe" # ipmitool 的完整路径 (Windows示例)
                                      # Linux/macOS: 可能为 "ipmitool" (如果已在系统PATH中)

# 新的风扇控制逻辑配置
TEMP_LOW_THRESHOLD_NEW = 60  # 低于此温度，设置低风扇转速
TEMP_AUTO_THRESHOLD_NEW = 70 # 高于或等于此温度，设置为自动模式
                             # 介于两者之间为中等转速

FAN_SPEED_LOW_HEX_NEW = "0x0A"  # 10% (十六进制的10是0x0A)
FAN_SPEED_MEDIUM_HEX_NEW = "0x32" # 50% (十六进制的50是0x32)
# 当温度 >= TEMP_AUTO_THRESHOLD_NEW 时，我们将启用iDRAC的自动模式

# IPMI 原始命令参数列表 (戴尔特定 - 请为您的服务器型号确认这些命令)
DELL_DISABLE_AUTO_FAN_CMD_ARGS = ["raw", "0x30", "0x30", "0x01", "0x00"] # 禁用自动，启用手动
DELL_ENABLE_AUTO_FAN_CMD_ARGS = ["raw", "0x30", "0x30", "0x01", "0x01"]  # 启用自动
DELL_SET_FAN_SPEED_PREFIX_CMD_ARGS = ["raw", "0x30", "0x30", "0x02", "0xff"] # 设置所有风扇速度的前缀

# --- 全局状态变量 (简单状态跟踪) ---
# None = 未知, False = 自动模式, True = 手动模式 (由脚本设置)
# 这个状态帮助减少不必要的IPMI命令，并不实时查询iDRAC
idrac_fan_mode_is_manual = None

# --- 函数定义 ---
def get_cpu_package_temp_from_ohm():
    """
    从本地OpenHardwareMonitor的Web服务获取CPU Package温度。
    如果有多个CPU，则返回最高的Package温度。
    """
    # print(f"OHM: 正在从 ({OHM_URL}) 获取CPU温度...") # 循环中打印太频繁，移至主循环
    try:
        response = requests.get(OHM_URL, timeout=4) # 较短超时，因为会频繁调用
        response.raise_for_status() # 如果状态码不是2xx，则引发HTTPError
        data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"OHM错误: 无法连接或获取数据: {e}")
        return None
    except ValueError as e: # 在较新版本的requests中是 requests.exceptions.JSONDecodeError
        print(f"OHM错误: 解析JSON数据失败: {e}")
        return None

    package_temps = []
    if not data.get('Children') or not isinstance(data['Children'], list) or not data['Children']:
        print("OHM错误: JSON结构不符合预期 (顶层Children缺失或为空).")
        return None
    
    computer_node = data['Children'][0] # 假设顶层Children的第一个元素是计算机节点
    if not computer_node.get('Children') or not isinstance(computer_node['Children'], list):
        print("OHM错误: JSON结构不符合预期 (计算机节点的Children缺失或非列表).")
        return None

    hardware_list = computer_node['Children']
    for hardware_item in hardware_list:
        is_cpu = False
        hardware_text_lower = hardware_item.get('Text', '').lower()
        # 尝试通过文本或ImageURL识别CPU
        if "cpu" in hardware_text_lower or \
           "intel" in hardware_text_lower or \
           "amd" in hardware_text_lower or \
           (hardware_item.get('ImageURL', '').endswith('cpu.png')):
            is_cpu = True

        if is_cpu:
            for component in hardware_item.get('Children', []):
                if component.get('Text', '') == 'Temperatures':
                    for temp_sensor in component.get('Children', []):
                        if temp_sensor.get('Text', '') == 'CPU Package' and 'Value' in temp_sensor:
                            try:
                                temp_str = temp_sensor['Value'].split()[0] # 例如 "45.0 °C" -> "45.0"
                                temp_val = float(temp_str)
                                package_temps.append(temp_val)
                                break # 找到此CPU的Package温度即可，移至下一个硬件项
                            except (ValueError, IndexError, TypeError):
                                print(f"    OHM警告: 解析CPU Package温度失败: '{temp_sensor.get('Value', 'N/A')}' 来自 '{hardware_item.get('Text', '未知CPU')}'")
    
    if not package_temps:
        print("OHM错误: 未能找到任何CPU Package温度。")
        print("  请确保OpenHardwareMonitor正在运行，Web服务器已启用，并且能检测到CPU Package温度。")
        return None

    max_package_temp = max(package_temps)
    # print(f"OHM: -> 获取到的本地CPU Package最高温度: {max_package_temp:.1f}°C") # 移至主循环打印
    return max_package_temp

def run_ipmi_command(command_args, expect_output=True):
    """
    执行ipmitool命令。
    如果 expect_output 为 True, 返回命令的标准输出。
    如果 expect_output 为 False, 命令成功执行则返回 True，否则返回 False。
    """
    base_cmd = [
        IPMITOOL_PATH, "-I", "lanplus", "-H", IDRAC_IP,
        "-U", IDRAC_USER, "-P", IDRAC_PASSWORD
    ]
    full_cmd = base_cmd + command_args
    try:
        timeout_duration = 15 # 对于控制命令，超时可以短一些
        if "sdr" in command_args: timeout_duration = 45 # sdr列表可能需要更长时间
        
        result = subprocess.run(
            full_cmd, capture_output=True, text=True, check=True, timeout=timeout_duration
        )
        return result.stdout.strip() if expect_output else True
    except subprocess.CalledProcessError as e:
        stderr_output = e.stderr.strip() if e.stderr else ""
        # 特殊处理：某些Dell iDRAC在成功执行某些raw OEM命令后，stderr为空但返回非零退出码。
        # 如果不期望输出（如设置命令），并且stderr为空，我们可能仍认为它已尝试执行。
        if not expect_output and not stderr_output and "raw" in e.cmd :
            print(f"IPMI信息: 命令 {' '.join(e.cmd)} 返回码 {e.returncode} 但stderr为空，可能已执行。")
            return True # 假设已执行（需要用户根据实际情况判断此行为是否可接受）

        print(f"IPMI命令执行错误 (CalledProcessError):")
        if e.cmd: print(f"  命令: {' '.join(e.cmd)}")
        if hasattr(e, 'returncode'): print(f"  退出码: {e.returncode}")
        if e.stdout: print(f"  标准输出: {e.stdout.strip()}")
        if stderr_output: print(f"  标准错误: {stderr_output}")

    except subprocess.TimeoutExpired:
        print(f"IPMI命令超时: {' '.join(full_cmd)}")
    except FileNotFoundError:
        print(f"错误: 找不到 ipmitool 可执行文件 '{IPMITOOL_PATH}'. 请检查路径。")
    except Exception as e:
        print(f"执行IPMI命令时发生未知错误: {e} (命令: {' '.join(full_cmd)})")
    return None if expect_output else False

def set_idrac_fan_mode_auto():
    """将iDRAC风扇设置为自动模式"""
    global idrac_fan_mode_is_manual
    print("iDRAC: 尝试将风扇控制设置为自动模式...")
    if run_ipmi_command(DELL_ENABLE_AUTO_FAN_CMD_ARGS, expect_output=False):
        print("iDRAC: 风扇控制已成功设置为自动模式。")
        idrac_fan_mode_is_manual = False # 更新脚本内部状态
        return True
    else:
        print("iDRAC错误: 设置风扇控制为自动模式失败。")
        idrac_fan_mode_is_manual = None # 状态未知，因为设置失败
        return False

def ensure_idrac_fan_mode_manual():
    """确保iDRAC风扇设置为手动模式，如果已经是（根据脚本状态）则不重复操作"""
    global idrac_fan_mode_is_manual
    if idrac_fan_mode_is_manual is True: # 检查脚本内部维护的状态
        # print("iDRAC: 风扇已处于手动模式 (根据脚本状态)。") # 调试时可取消注释
        return True
    
    print("iDRAC: 尝试将风扇控制设置为手动模式 (禁用自动)...")
    if run_ipmi_command(DELL_DISABLE_AUTO_FAN_CMD_ARGS, expect_output=False):
        print("iDRAC: 风扇控制已成功设置为手动模式。")
        idrac_fan_mode_is_manual = True # 更新脚本内部状态
        return True
    else:
        print("iDRAC错误: 设置风扇控制为手动模式失败。")
        idrac_fan_mode_is_manual = None # 状态未知
        return False

def set_idrac_fan_speed_percentage(speed_hex):
    """通过iDRAC设置风扇速度百分比 (十六进制)"""
    try:
        speed_decimal = int(speed_hex, 16)
        # print(f"iDRAC: 准备设置风扇速度为 {speed_hex} (~{speed_decimal}%)...") # 移到主循环打印
    except ValueError:
        print(f"错误: 无效的风扇速度十六进制值 '{speed_hex}'")
        return False

    # 在设置手动速度前，确保iDRAC处于手动模式
    if not ensure_idrac_fan_mode_manual(): # 这个函数会尝试设置并更新状态
        print("iDRAC错误: 未能进入手动风扇模式，无法设置风扇速度百分比。")
        return False
        
    command_args = DELL_SET_FAN_SPEED_PREFIX_CMD_ARGS + [speed_hex]
    if run_ipmi_command(command_args, expect_output=False):
        # print(f"iDRAC: 风扇速度已成功设置为 {speed_hex} (~{speed_decimal}%).") # 移到主循环打印
        return True
    else:
        print(f"iDRAC错误: 设置风扇速度 {speed_hex} 失败。")
        return False

# --- 主控制循环 ---
def main_control_loop():
    print("启动风扇控制脚本 (循环运行)...")
    print(f"  检测间隔: {SCRIPT_LOOP_INTERVAL_SECONDS} 秒")
    print(f"  温度来源: 本地 OpenHardwareMonitor ({OHM_URL})")
    print(f"  风扇控制目标: 远程 iDRAC ({IDRAC_IP})")
    print("  重要警告:")
    print("    1. 此脚本使用本地机器的CPU温度来控制【远程iDRAC服务器】的风扇。")
    print("       请务必确认此逻辑符合您的实际需求和风险评估。")
    print("    2. 根据您的要求，脚本退出时，如果最后检测的本地温度低于70°C，")
    print("       iDRAC风扇将保持在最后的手动设置状态，而【不会】自动恢复到iDRAC的自动模式。")
    print("       这可能在脚本停止后导致服务器过热，请谨慎使用并监控服务器。")
    print("  按 Ctrl+C 退出脚本。")
    print("-" * 60)

    global idrac_fan_mode_is_manual # 允许修改全局状态变量
    idrac_fan_mode_is_manual = None  # 初始状态未知

    while True:
        current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        print(f"\n--- 时间: {current_time} ---")

        local_cpu_temp = get_cpu_package_temp_from_ohm()

        if local_cpu_temp is not None:
            print(f"本地OHM CPU Package温度: {local_cpu_temp:.1f}°C")
            if local_cpu_temp >= TEMP_AUTO_THRESHOLD_NEW:
                print(f"  决策: 本地温度 ({local_cpu_temp:.1f}°C) >= {TEMP_AUTO_THRESHOLD_NEW}°C. iDRAC目标: 自动风扇模式。")
                set_idrac_fan_mode_auto()
            else:
                # 温度低于自动阈值，需要手动设置百分比
                target_speed_hex = None
                if local_cpu_temp < TEMP_LOW_THRESHOLD_NEW:
                    target_speed_hex = FAN_SPEED_LOW_HEX_NEW
                    print(f"  决策: 本地温度 ({local_cpu_temp:.1f}°C) < {TEMP_LOW_THRESHOLD_NEW}°C. iDRAC目标: 低速 ({target_speed_hex}).")
                else: # TEMP_LOW_THRESHOLD_NEW <= local_cpu_temp < TEMP_AUTO_THRESHOLD_NEW
                    target_speed_hex = FAN_SPEED_MEDIUM_HEX_NEW
                    print(f"  决策: 本地温度 ({local_cpu_temp:.1f}°C) 在 {TEMP_LOW_THRESHOLD_NEW}-{TEMP_AUTO_THRESHOLD_NEW}°C 之间. iDRAC目标: 中速 ({target_speed_hex}).")
                
                # ensure_idrac_fan_mode_manual() 会在 set_idrac_fan_speed_percentage 内部被调用
                if set_idrac_fan_speed_percentage(target_speed_hex):
                     print(f"  iDRAC: 风扇速度已设置为 {target_speed_hex} (~{int(target_speed_hex,16)}%).")
                # else: # 失败信息会在函数内部打印
        else:
            print("未能从OpenHardwareMonitor获取有效温度。本次不调整iDRAC风扇设置。")
            print("  iDRAC风扇将保持当前状态。")
            # 考虑：如果连续N次获取温度失败，是否应强制iDRAC进入自动模式？ (当前未实现)

        print(f"等待 {SCRIPT_LOOP_INTERVAL_SECONDS} 秒...")
        time.sleep(SCRIPT_LOOP_INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        main_control_loop()
    except KeyboardInterrupt:
        print("\n检测到用户中断 (Ctrl+C)。正在执行退出清理...")
    except Exception as e:
        print(f"\n脚本主循环发生未捕获的严重错误: {e}")
        print("正在执行退出清理...")
    finally:
        print("-" * 30)
        print("脚本即将退出...")
        
        print("正在获取最后一次本地CPU温度以决定iDRAC风扇最终状态...")
        # 注意: get_cpu_package_temp_from_ohm() 内部有打印，这里不再重复打印获取过程
        last_known_local_temp = get_cpu_package_temp_from_ohm()

        if last_known_local_temp is not None:
            print(f"最后检测到的本地CPU温度为: {last_known_local_temp:.1f}°C")
            if last_known_local_temp >= TEMP_AUTO_THRESHOLD_NEW:
                print(f"  最后温度 ({last_known_local_temp:.1f}°C) >= {TEMP_AUTO_THRESHOLD_NEW}°C。确保iDRAC为自动风扇模式。")
                set_idrac_fan_mode_auto() # 函数内部会打印成功/失败
            else:
                print(f"  最后温度 ({last_known_local_temp:.1f}°C) < {TEMP_AUTO_THRESHOLD_NEW}°C。根据您的要求，iDRAC风扇将【保持当前设置】。")
                print("  安全警告: iDRAC服务器风扇将保持在脚本退出前的最后手动设置状态。")
                print("  如果脚本不再运行，请务必监控服务器温度以防过热。")
        else:
            print("警告: 退出时无法获取本地CPU温度。")
            print("  为安全起见，将强制尝试设置iDRAC为自动风扇模式。")
            set_idrac_fan_mode_auto() # 函数内部会打印成功/失败
        
        print("-" * 30)
        print("风扇控制脚本已结束。")