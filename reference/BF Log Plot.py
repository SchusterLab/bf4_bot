# BF Log Plot
# 8/14/2024, by Chunyang Ding
import matplotlib.pyplot as plt
import numpy as np
from datetime import date, datetime, time, timedelta


# Collate data over date range from log file
def collate_data_pressure(start_date, end_date, file_prefix="S:/_BlueFors4/Logs/"):
    date_time = []
    pressure = []
    # Iterate through every date and create proper file name
    for i in range((end_date - start_date).days + 1):
        iter_date = start_date + timedelta(i)
        date_str = iter_date.strftime("%y-%m-%d")
        filename = file_prefix + date_str + "/maxigauge " + date_str + ".log"

        # Open file and read CSV data
        try:
            with open(filename, "r") as file:
                for line in file:
                    data = line.split(",")
                    if len(data) > 1:
                        bad_date = data[0]
                        day = int(bad_date[0:2])
                        month = int(bad_date[3:5])
                        year = int(bad_date[6:8]) + 2000
                        d = date(year, month, day)
                        t = time.fromisoformat(data[1])
                        date_time.append(datetime.combine(d, t))
                        pressure.append(
                            [
                                float(data[5]),
                                float(data[11]),
                                float(data[17]),
                                float(data[23]),
                                float(data[29]),
                                float(data[35]),
                            ]
                        )
        except:
            print("File not found: " + filename)
            continue
    # Change date_time into hours since first date_time
    date_time_hours = [(x - date_time[0]).total_seconds() / 3600 for x in date_time]

    return date_time_hours, pressure


# Collate data over date range from log file
def collate_data_temp(start_date, end_date, channel, file_prefix="S:/_BlueFors4/Logs/"):
    date_time = []
    temperature = []
    # Iterate through every date and create proper file name
    for i in range((end_date - start_date).days + 1):
        iter_date = start_date + timedelta(i)
        # file_prefix = "S:/_BlueFors4/Logs/"
        date_str = iter_date.strftime("%y-%m-%d")
        filename = (
            file_prefix + date_str + "/CH" + str(channel) + " T " + date_str + ".log"
        )

        # Open file and read CSV data
        try:
            with open(filename, "r") as file:
                for line in file:
                    data = line.split(",")
                    if len(data) > 1:
                        data[0] = data[0].lstrip()
                        bad_date = data[0]
                        day = int(bad_date[0:2])
                        month = int(bad_date[3:5])
                        year = int(bad_date[6:8]) + 2000
                        d = date(year, month, day)
                        t = time.fromisoformat(data[1])
                        date_time.append(datetime.combine(d, t))
                        temperature.append(float(data[2]))
        except:
            print("File not found: " + filename)
            continue
    # Change date_time into hours since first date_time
    date_time_hours = [(x - date_time[0]).total_seconds() / 3600 for x in date_time]
    if len(date_time_hours) != len(temperature):
        print(
            "Data length mismatch: " + len(date_time_hours) + " vs " + len(temperature)
        )
        temperature = temperature[: len(date_time_hours)]
    return date_time_hours, temperature


# Set date range
start_date = date(2025, 5, 30)
end_date = date(2025, 5, 31)

# start_date = date(2025, 7, 27)
# end_date = date(2025, 7, 31)


# file_prefix = "S:/_BlueFors4/Logs/"
file_prefix = "S:/_BlueFors/LogPost2023/"
time_list, pressure = collate_data_pressure(start_date, end_date, file_prefix=file_prefix)

# Plot data
plt.close()
plt.figure(figsize=(14, 8))
ax = plt.subplot(
    121,
    xlabel="Time (days since start)",
    ylabel="Pressure (mbar)",
    title="BF Pressure Log from "
    + start_date.strftime("%y-%m-%d")
    + " to "
    + end_date.strftime("%y-%m-%d"),
)
ax.set_yscale("log")
ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 0], label='P1')  #OVC
ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 1], label="P2")  # Before turbo
ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 2], label="P3")  # After cold trap, before DU
ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 3], label="P4")  # Before cold trap
# ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 4], label="P5")  # Tank pressure
# ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 3] - np.array(pressure)[:, 2], label='P4 - P3')  #After cold trap, before DU
# ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 5], label="P6")  # Maintenance manifold pressure
plt.legend()
# plt.show()

time_list_50K, temp_50K = collate_data_temp(start_date, end_date, 1, file_prefix=file_prefix)
time_list_4K, temp_4K = collate_data_temp(start_date, end_date, 2, file_prefix=file_prefix)
time_list_still, temp_still = collate_data_temp(start_date, end_date, 5, file_prefix=file_prefix)
time_list_mxc, temp_mxc = collate_data_temp(start_date, end_date, 6, file_prefix=file_prefix)

# Plot data
# plt.close()
# plt.figure(figsize=(8, 8))
ax = plt.subplot(
    122,
    xlabel="Time (days since start)",
    ylabel="Temperature (K)",
    title="BF Temperature Log from "
    + start_date.strftime("%y-%m-%d")
    + " to "
    + end_date.strftime("%y-%m-%d"),
)
# set y axis to log scale
ax.set_yscale("log")
ax.plot(np.array(time_list_mxc) / 24, np.array(temp_mxc), label="MXC")
ax.plot(np.array(time_list_still) / 24, np.array(temp_still), label="Still")
ax.plot(np.array(time_list_4K) / 24, np.array(temp_4K), label="4K")
ax.plot(np.array(time_list_50K) / 24, np.array(temp_50K), label="50K")
plt.legend()
plt.show()


# #Plot data
# plt.close()
# plt.figure(figsize=(14, 8))
# ax = plt.subplot(111, xlabel='Time (days since start)', ylabel='Pressure (mbar)',
#                  title='BF Pressure Log from ' + start_date.strftime("%y-%m-%d") + ' to ' + end_date.strftime(
#                      "%y-%m-%d"))
# # ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 0], label='P1')  #OVC
# # ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 1], label='P2')  #Before turbo
# ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 2], '--', label='P3')  #After cold trap, before DU
# # ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 3], label='P4')  #Before cold trap
# # ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 4], label='P5')  #Tank pressure
# # ax.plot(np.array(time_list) / 24, np.array(pressure)[:, 5], label='P6')  #Maintenance manifold pressure
# # plt.legend()
# # plt.show()
# plt.axvline(0.836)
# # plt.axvline(0.875)
# # plt.axvline(0.885)
# plt.legend()
# ax2 = ax.twinx()
#
# time_list_50K, temp_50K = collate_data_temp(start_date, end_date, 1)
# time_list_4K, temp_4K = collate_data_temp(start_date, end_date, 2)
# time_list_still, temp_still = collate_data_temp(start_date, end_date, 5)
# time_list_mxc, temp_mxc = collate_data_temp(start_date, end_date, 6)
#
# #Plot data
# # plt.close()
# # plt.figure(figsize=(8, 8))
# # ax = plt.subplot(111, xlabel='Time (days since start)', ylabel='Temperature (K)',
# #                  title='BF Temperature Log from ' + start_date.strftime("%y-%m-%d") + ' to ' + end_date.strftime(
# #                      "%y-%m-%d"))
# # set y axis to log scale
# # ax.set_yscale('log')
# ax2.plot(np.array(time_list_mxc) / 24, np.array(temp_mxc), label='MXC')
# ax2.plot(np.array(time_list_still) / 24, np.array(temp_still), label='Still')
# ax2.plot(np.array(time_list_4K) / 24, np.array(temp_4K), label='4K')
# ax2.plot(np.array(time_list_50K) / 24, np.array(temp_50K), label='50K')
# plt.axhline(77, c = 'k', linestyle='--', label='LN2 temperature')
# ax2.set_ylabel('Temperature [K]')
# plt.legend()
# plt.show()
