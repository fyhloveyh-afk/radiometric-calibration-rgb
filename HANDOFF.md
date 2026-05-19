# RGB 可见光相机辐射标定项目交接说明

## 项目背景

当前项目目录：

```text
E:\标定
```

目标是给一台 RGB 可见光相机做辐射标定。相机已知 R/G/B 三通道光谱响应，并且前端装有 650 nm 截止滤光片。标定思想参考了一份短波红外相机黑体炉标定报告，但这里已按可见光 RGB 相机重新整理。

核心标定关系是分别建立三条通道曲线：

```text
L_eff_R = f_R((DN_R - Dark_R) / t)
L_eff_G = f_G((DN_G - Dark_G) / t)
L_eff_B = f_B((DN_B - Dark_B) / t)
```

其中：

```text
DN_c      有光图像中 c 通道 ROI 平均灰度
Dark_c    相同曝光时间下 c 通道暗场 ROI 平均灰度
t         曝光时间，单位 s
L_eff_c   c 通道等效辐亮度或通道积分辐射
c         R, G, B
```

注意：这里的 `L_eff_c` 不是单波长辐亮度，而是经过相机通道光谱响应和滤光片透过率加权后的通道等效辐射。


## 光谱响应和滤光片

用户提供的相机光谱响应文件：

```text
\\wsl.localhost\Ubuntu-22.04\home\yuhao\数据模拟\spectral_response_full.csv
```

文件列结构：

```text
wavelength_nm,mono,green,blue,red
```

脚本使用 `red / green / blue` 三列，`mono` 暂不参与 RGB 标定。

650 nm 截止滤光片默认按理想短波通过处理：

```text
T_filter(lambda) = 1, lambda <= 650 nm
T_filter(lambda) = 0, lambda > 650 nm
```

如果后续有真实滤光片透过率曲线，可以使用 `--filter-csv` 参数替换理想截止模型。

三通道等效响应定义为：

```text
S_eff_c(lambda) = S_c(lambda) * T_filter(lambda)
```


## 关键物理公式

### 1. 黑体光谱辐亮度

脚本已内置普朗克黑体辐射定律，按波长形式计算：

```text
L_lambda(lambda, T) =
epsilon * [2 h c^2 / lambda^5] /
[exp(h c / (lambda k T)) - 1]
```

物理常数：

```text
h = 6.62607015e-34 J*s
c = 299792458.0 m/s
k = 1.380649e-23 J/K
```

脚本内部先以 `W/(m2 sr m)` 计算，再乘 `1e-9` 转成：

```text
W/(m2 sr nm)
```

### 2. 通道等效辐亮度

默认 `--reference-kind normalized`，计算：

```text
L_eff_c =
integral( L_lambda(lambda) * S_eff_c(lambda) d_lambda )
/
integral( S_eff_c(lambda) d_lambda )
```

这表示通道响应加权后的等效平均辐亮度。

如果使用 `--reference-kind integrated`，则计算：

```text
E_c = integral( L_lambda(lambda) * S_eff_c(lambda) d_lambda )
```

这表示通道积分辐射。两种方式都可以，但一次标定必须保持一致。


## 当前脚本

主脚本：

```text
radiometric_calibration_rgb.py
```

脚本支持三种参考辐射输入方式：

1. 测量表直接提供 `l_eff_r / l_eff_g / l_eff_b`
2. 测量表提供 `spectrum_file`，脚本读取光谱辐亮度文件并自动积分
3. 测量表提供 `blackbody_temp_c` 或 `blackbody_temp_k`，脚本按普朗克定律生成黑体光谱，再自动积分

参考辐射补全逻辑在函数：

```python
add_reference_columns(...)
```

黑体光谱计算函数：

```python
blackbody_spectral_radiance_nm(...)
```

光谱文件积分函数：

```python
integrate_channel_reference(...)
```

通用光谱加权积分函数：

```python
integrate_radiance_reference(...)
```


## 输入数据格式

### 方式 A：直接填写 l_eff_r/g/b

模板：

```text
calibration_measurements_template.csv
```

格式：

```csv
level,exposure_ms,dn_r,dn_g,dn_b,dark_r,dark_g,dark_b,l_eff_r,l_eff_g,l_eff_b
L1,1.0,1200,1500,900,64,62,65,1.23,1.18,0.95
```

### 方式 B：填写光谱辐亮度文件

光谱文件模板：

```text
source_spectrum_template.csv
```

测量表格式：

```csv
level,exposure_ms,dn_r,dn_g,dn_b,dark_r,dark_g,dark_b,spectrum_file
L1,1.0,1200,1500,900,64,62,65,spectra/L1.csv
```

每个 `spectrum_file` 需要包含：

```csv
wavelength_nm,radiance
380,0.10
400,0.12
```

### 方式 C：填写黑体炉温度

模板：

```text
blackbody_measurements_template.csv
```

格式：

```csv
level,blackbody_temp_c,emissivity,exposure_ms,dn_r,dn_g,dn_b,dark_r,dark_g,dark_b
T900,900,1.0,1.0,900,650,260,64,62,65
```

也可以用 `blackbody_temp_k` 代替 `blackbody_temp_c`。如果同时存在，脚本优先使用 `blackbody_temp_k`。

`emissivity` 默认值为 `1.0`，当前按灰体常数处理。如果后续有随波长变化的发射率，需要进一步扩展为 `emissivity(lambda)`。


## 常用运行命令

### 黑体炉温度标定

```powershell
python .\radiometric_calibration_rgb.py `
  --measurements .\blackbody_measurements_template.csv `
  --spectral-response "\\wsl.localhost\Ubuntu-22.04\home\yuhao\数据模拟\spectral_response_full.csv" `
  --cutoff-nm 650 `
  --reference-kind normalized `
  --model linear `
  --output-dir .\calibration_output_blackbody
```

### 光谱文件自动积分标定

```powershell
python .\radiometric_calibration_rgb.py `
  --measurements .\measurements_with_spectrum.csv `
  --spectral-response "\\wsl.localhost\Ubuntu-22.04\home\yuhao\数据模拟\spectral_response_full.csv" `
  --cutoff-nm 650 `
  --reference-kind normalized `
  --model linear `
  --output-dir .\calibration_output
```

### 真实滤光片曲线

```powershell
python .\radiometric_calibration_rgb.py `
  --measurements .\blackbody_measurements_template.csv `
  --spectral-response "\\wsl.localhost\Ubuntu-22.04\home\yuhao\数据模拟\spectral_response_full.csv" `
  --filter-csv .\filter_transmission.csv `
  --reference-kind normalized `
  --model linear `
  --output-dir .\calibration_output_blackbody
```


## 拟合模型

推荐优先使用线性模型：

```text
--model linear
```

对应：

```text
L_eff_c = a_c * X_c + b_c
X_c = (DN_c - Dark_c) / exposure_s
```

如果响应非线性明显，可以试：

```text
--model poly --degree 2
--model logpoly --degree 2
```

不要一开始就使用高阶多项式，容易过拟合。


## 输出文件

脚本输出目录由 `--output-dir` 指定，常见输出包括：

```text
effective_response.csv
effective_response.png
calibration_fit_table.csv
calibration_coefficients.json
calibration_report.md
fit_r.png
fit_g.png
fit_b.png
residual_r.png
residual_g.png
residual_b.png
```

其中：

```text
calibration_fit_table.csv
```

会包含自动补全的：

```text
l_eff_r,l_eff_g,l_eff_b
```

以及：

```text
dn_corr_r,x_r,ref_r
dn_corr_g,x_g,ref_g
dn_corr_b,x_b,ref_b
```


## Git 状态

当前目录已初始化为 Git 仓库。

第一版提交：

```text
20fdc87 Initial RGB radiometric calibration script
```

已提交文件：

```text
.gitignore
README.txt
HANDOFF.md
radiometric_calibration_rgb.py
calibration_measurements_template.csv
blackbody_measurements_template.csv
source_spectrum_template.csv
```

注意：`HANDOFF.md` 是在第一版提交后新增的，复制到另一台电脑后如果需要保存，应再执行一次 Git 提交。


## .gitignore 说明

以下内容不建议提交：

```text
calibration_output*/
report_media/
extracted_report_text.txt
__pycache__/
*.pyc
```

这些是测试输出、中间文件或可再生成内容。


## 后续注意事项

1. 如果要做严格辐射标定，尽量使用 RAW 或线性 RGB 图像，不要使用 JPEG/sRGB。
2. 自动曝光、自动增益、自动白平衡、Gamma、HDR、降噪、锐化、色彩增强都应关闭或固定。
3. 每个曝光时间都要拍对应暗场，不能只用一个暗场代替全部曝光。
4. 标定点要避开过暗和饱和区域。
5. 同一温度或亮度等级下，`DN - Dark` 应该随曝光时间近似线性。
6. 红通道受 650 nm 截止滤光片影响最大，不能忽略滤光片。
7. 当前黑体计算假设发射率是常数；如果黑体炉或目标不是理想黑体，应根据实际发射率修正。


## 给另一台 Codex 的接手建议

另一台电脑打开本目录后，建议先执行：

```powershell
git log --oneline -5
git status --short
Get-Content .\README.txt -TotalCount 80
Get-Content .\HANDOFF.md -TotalCount 120
```

然后检查 Python 环境是否有：

```text
numpy
pandas
matplotlib
```

再运行黑体模板测试：

```powershell
python .\radiometric_calibration_rgb.py `
  --measurements .\blackbody_measurements_template.csv `
  --spectral-response "\\wsl.localhost\Ubuntu-22.04\home\yuhao\数据模拟\spectral_response_full.csv" `
  --cutoff-nm 650 `
  --reference-kind normalized `
  --model linear `
  --output-dir .\calibration_output_blackbody_test
```

如果另一台电脑没有同样的 WSL 路径，需要把 `spectral_response_full.csv` 一起复制过去，并修改 `--spectral-response` 参数。

