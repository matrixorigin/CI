import re
import os
import sys
import logging
import argparse

# 设置日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(filename)s - line %(lineno)d - %(levelname)s - %(message)s'
)

def merge_coverage_files(output_path, *coverage_files):
    """合并多个coverage.out文件，代码块只要在一个文件中命中就算命中，并计算覆盖率"""

    merged_coverage = {}
    total_blocks = 0  # 使用计数器来避免重复计数
    covered_blocks = 0
    
    for coverage_file in coverage_files:
        try:
            with open(coverage_file, 'r') as cov_file:
                for line in cov_file:
                    
                    if line.startswith('mode:'):
                        continue
                    
                    parts = line.split(':')
                    if len(parts) < 2:
                        continue
                    
                    file_name = parts[0]
                    line_info_and_counts = parts[1].split()
                    line_info = line_info_and_counts[0]
                    block_size = line_info_and_counts[1]
                    executed_count = int(line_info_and_counts[-1])  # 提取最后一个字段作为 executed_count
                    
                    # 使用文件名和行信息作为唯一键
                    key = (file_name, line_info, block_size)
                    
                    # 确保每个代码块只计数一次
                    if key not in merged_coverage:
                        merged_coverage[key] = 1 if executed_count>0 else 0
                        total_blocks += 1
                        if executed_count > 0:
                            covered_blocks += 1
                    else:
                        if executed_count > 0 and merged_coverage[key] == 0:
                            merged_coverage[key] = 1
                            covered_blocks += 1
        
        except Exception as e:
            logging.error(f"Error reading coverage file {coverage_file}: {e}")
            raise

    # 计算覆盖率
    coverage_percentage = (covered_blocks / total_blocks) if total_blocks > 0 else 0
    logging.info(f"Total code blocks: {total_blocks}, Covered blocks: {covered_blocks}, Coverage: {coverage_percentage}%")

    # 将合并后的结果写入输出文件
    try:
        with open(output_path, 'w') as out_file:
            out_file.write('mode: set\n')
            for key, executed_count in merged_coverage.items():
                file_name, line_info, block_size = key
                out_file.write(f'{file_name}:{line_info} {block_size} {executed_count}\n')  # `0` can be the default number for ignored value
    except Exception as e:
        logging.error(f"Error writing merged coverage file: {e}")
        raise

    logging.info(f"Successfully merged coverage files into {output_path}")
    
    return total_blocks, covered_blocks, coverage_percentage

def parse_diff(diff_path):
    """解析diff文件，获取新增和修改的行号和列范围，仅处理 .go 文件"""
    logging.info(f"Starting to parse diff file: {diff_path}")
    modified_lines = {}

    current_file = None
    current_line_number = None

    try:
        with open(diff_path, 'r') as diff_file:
            for line in diff_file:
                if line.startswith('+++ b/'):
                    current_file = normalize_path(line[6:].strip(), '')
                    if not current_file.endswith('.go'):
                        logging.info(f"Ignoring non-Go file: {current_file}")
                        current_file = None  # 忽略非 .go 文件
                        continue
                    elif current_file.endswith('.pb.go'):
                        logging.info(f"Ignoring auto-generated pb.go file: {current_file}")
                        current_file = None  # 忽略自动生成的 .go 文件
                        continue
                    elif 'pkg/frontend/test' in current_file:
                        logging.info(f"Ignoring auto-generated test go file: {current_file}")
                        current_file = None  # 忽略自动生成的 .go 文件
                        continue
                    elif 'pkg/vm/engine/tae/db/testutil' in current_file:
                        logging.info(f"Ignoring test go file: {current_file}")
                        current_file = None  # 忽略测试 .go 文件
                        continue
                    elif 'pkg/vm/engine/test' in current_file:
                        logging.info(f"Ignoring test go file: {current_file}")
                        current_file = None  # 忽略测试 .go 文件
                        continue
                    elif 'pkg/tests/service' in current_file:
                        logging.info(f"Ignoring test go file: {current_file}")
                        current_file = None  # 忽略测试 .go 文件
                        continue
                    elif 'pkg/tests/txn' in current_file:
                        logging.info(f"Ignoring test go file: {current_file}")
                        current_file = None  # 忽略测试 .go 文件
                        continue
                    elif 'pkg/tests/upgrade' in current_file:
                        logging.info(f"Ignoring test go file: {current_file}")
                        current_file = None  # 忽略测试 .go 文件
                        continue
                    elif 'pkg/util/metric' in current_file:
                        logging.info(f"Ignoring test go file: {current_file}")
                        current_file = None  # 忽略测试 .go 文件
                        continue
                    elif 'pkg/common/morpc/examples' in current_file:
                        logging.info(f"Ignoring test go file: {current_file}")
                        current_file = None  # 忽略测试 .go 文件
                        continue
                    # 没有参与ut测试的文件进行忽略
                    elif 'driver' in current_file:
                        logging.info(f"Ignoring test go file: {current_file}")
                        current_file = None  # 忽略测试 .go 文件
                        continue
                    elif 'engine/aoe' in current_file:
                        logging.info(f"Ignoring test go file: {current_file}")
                        current_file = None  # 忽略测试 .go 文件
                        continue
                    elif 'engine/memEngine' in current_file:
                        logging.info(f"Ignoring test go file: {current_file}")
                        current_file = None  # 忽略测试 .go 文件
                        continue
                    elif 'pkg/catalog' in current_file:
                        logging.info(f"Ignoring test go file: {current_file}")
                        current_file = None  # 忽略测试 .go 文件
                        continue
                    logging.info(f"Processing file: {current_file}")
                
                elif line.startswith('@@ ') and current_file:
                    # 解析 @@ 行，获取新文件的起始行号
                    match = re.match(r'@@ -\d+,\d+ \+(\d+),\d+ @@', line)
                    if match:
                        current_line_number = int(match.group(1))  # 新增代码块的起始行号
                        logging.info(f"New file starting line number: {current_line_number}")
                
                elif line.startswith('+') and current_file:
                    # 处理新增和修改的行，确保它是有效代码
                    if current_line_number is not None:
                        # 获取当前行内容
                        new_line = line[1:]
                        
                        # 计算第一个和最后一个有效代码字符的位置
                        stripped_line = new_line.strip()
                        if stripped_line:
                            first_non_space = new_line.index(stripped_line[0]) + 1
                            last_non_space = new_line.rindex(stripped_line[-1]) + 1

                            # 校正列号，忽略行首的结构符号和无效代码部分
                            while first_non_space <= last_non_space and not is_valid_code_segment(new_line[first_non_space - 1]):
                                first_non_space += 1

                            while last_non_space >= first_non_space and not is_valid_code_segment(new_line[last_non_space - 1]):
                                last_non_space -= 1
                            
                            # 只有在确实有有效代码的情况下才记录
                            if first_non_space <= last_non_space:
                                if current_file not in modified_lines:
                                    modified_lines[current_file] = []
                                modified_lines[current_file].append((current_line_number, first_non_space, last_non_space))

                        current_line_number += 1
                
                elif not line.startswith('-') and current_file:
                    # 处理未修改的行，递增行号
                    if current_line_number is not None:
                        current_line_number += 1
    except Exception as e:
        logging.error(f"Error parsing diff file: {e}")
        raise
    return modified_lines

def get_modified_columns(old_line, new_line):
    """快速获取行内的修改列号"""
    min_len = min(len(old_line), len(new_line))
    modified_columns = []

    # 查找修改的列号
    for i in range(min_len):
        if old_line[i] != new_line[i]:
            modified_columns.append(i + 1)
    
    # 如果新行更长，补充剩余部分的列号
    if len(new_line) > min_len:
        modified_columns.extend(range(min_len + 1, len(new_line) + 1))
    
    return modified_columns

def parse_coverage_and_generate_report(coverage_path, modified_lines, output_path='pr_coverage.out'):
    """解析coverage.out文件，计算覆盖率并生成覆盖率报告"""
    logging.info(f"Starting to parse coverage file: {coverage_path}")
    total_modified_blocks = 0
    covered_blocks = set()
    seen_blocks = set()  # 用于跟踪已经处理过的代码块范围

    logging.debug(f"[modified_lines]{modified_lines}")

    try:
        with open(output_path, 'w') as pr_cov_file:
            pr_cov_file.write('mode: set\n')

            with open(coverage_path, 'r') as cov_file:
                for line in cov_file:
                    if line.startswith('mode:'):
                        continue

                    parts = line.split(':')
                    if len(parts) < 2:
                        continue

                    file_name = normalize_path(parts[0])
                    line_info = parts[1].split()[0]
                    executed_count = int(parts[1].split()[-1])

                    # 处理行范围
                    line_range = line_info.split(',')
                    if len(line_range) != 2:
                        logging.warning(f"Unexpected line range format: {line_info}")
                        continue

                    try:
                        start_line = int(line_range[0].split('.')[0])
                        end_line = int(line_range[1].split('.')[0])
                        start_column = int(line_range[0].split('.')[1])
                        end_column = int(line_range[1].split('.')[1])
                    except ValueError as e:
                        logging.warning(f"Error parsing line range in coverage file: {e}")
                        continue

                    if (file_name, (start_line, end_line, start_column, end_column)) in seen_blocks:
                        continue

                    seen_blocks.add((file_name, (start_line, end_line, start_column, end_column)))

                    if file_name in modified_lines:
                        for modified_line, mincol, maxcol in modified_lines[file_name]:
                            # 判断是否在行范围内
                            if start_line < modified_line < end_line:
                                # 整行被覆盖，无需检查列号
                                if line not in covered_blocks:
                                    total_modified_blocks += 1
                                if executed_count > 0:
                                    covered_blocks.add(line)
                                pr_cov_file.write(line)
                                break
                            # 检查边界行的列号范围
                            elif modified_line == start_line and modified_line == end_line:
                                if not (mincol > end_column or maxcol < start_column):
                                    if line not in covered_blocks:
                                        total_modified_blocks += 1
                                    if executed_count > 0:
                                        covered_blocks.add(line)
                                    pr_cov_file.write(line)
                                    break
                            elif modified_line == start_line and modified_line != end_line:
                                if start_column < maxcol:
                                    if line not in covered_blocks:
                                        total_modified_blocks += 1
                                    if executed_count > 0:
                                        covered_blocks.add(line)
                                    pr_cov_file.write(line)
                                    break
                            elif modified_line == end_line and modified_line != start_line:
                                if end_column > mincol:
                                    if line not in covered_blocks:
                                        total_modified_blocks += 1
                                    if executed_count > 0:
                                        covered_blocks.add(line)
                                    pr_cov_file.write(line)
                                    break
                    
    except Exception as e:
        logging.error(f"Error parsing coverage file or generating report: {e}")
        raise

    logging.debug(f"[covered_lines][{total_modified_blocks}]"+'\n'.join(covered_blocks))

    coverage_percentage = (len(covered_blocks) / total_modified_blocks) if total_modified_blocks > 0 else 1   
    return total_modified_blocks, len(covered_blocks), coverage_percentage

def normalize_path(path, prefix='github.com/matrixorigin/matrixone/'):
    """移除特定前缀，规范化路径"""
    if path.startswith(prefix):
        return path[len(prefix):]
    return os.path.normpath(path)

def diff_coverage(diff_path, coverage_path, output_path='pr_coverage.out'):
    try:
        # 解析diff文件，获取修改和新增的行号
        modified_lines = parse_diff(diff_path)
        logging.debug(f"[modified_lines]{modified_lines}")

        if len(modified_lines) == 0:
            logging.info("No go file changes")
            return 0,0,1

        # 解析coverage.out文件，计算覆盖率并生成覆盖率报告
        total_modified_lines, covered_modified_lines, coverage_percentage = parse_coverage_and_generate_report(coverage_path, modified_lines, output_path)

        
        # 输出结果
        logging.info(f"PR Coverage Report Generated: {output_path}")
        return total_modified_lines, covered_modified_lines, coverage_percentage

    except Exception as e:
        logging.error(f"An error occurred during the process: {e}")
        return 0,0,0

def is_valid_code_segment(segment):
    """判断一个代码片段是否包含有效的代码（忽略结构符号、关键字和空白）"""
    stripped_segment = segment.strip()
    # 忽略单独的结构符号、关键字、控制语句等
    if stripped_segment in {'{', '}', '(', ')', 'else', 'if', 'for', 'while', 'switch', 'case', 'default'}:
        return False
    # 忽略空白行和注释行
    if not stripped_segment or stripped_segment.startswith("//") or stripped_segment.startswith("#"):
        return False
    return True

def parse_file_coverage(minimal_coverage,file='./pr_coverage.out'):
    try:
        exec_dict=dict()
        not_exec_dict=dict()
        with open(file, 'r') as f:
            for line in f:
                if line.startswith('mode:'):
                    continue
                parts=line.split(':')
                if len(parts) < 2:
                    continue
                file_name = normalize_path(parts[0])
                if not exec_dict.get(file_name):
                    exec_dict[file_name]=0
                    not_exec_dict[file_name]=0
                exec_status=int(parts[1].split()[-1])
                if exec_status > 0:
                    exec_dict[file_name]+=1
                else:
                    not_exec_dict[file_name]+=1
        if len(exec_dict) != len(not_exec_dict):
            logging.error("exec_dict and not_exec_dict length not match")
            return
        for i in exec_dict.keys():
            coverage = exec_dict[i] / (exec_dict[i] + not_exec_dict[i]) * 100
            if coverage < minimal_coverage*100:
              logging.warning(f"filename:{file_name} ,coverage {coverage}% is blow or equal {minimal_coverage}%")
              continue
            logging.info(f"filename:{i},  coverage:{coverage}%")

    except Exception as e:
        logging.error(f"An error parse_file_coverage: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Merge coverage files and calculate coverage based on diff.")

    parser.add_argument(
        '-coverage_files', 
        nargs='+', 
        required=True,
        help='List of coverage.out files to merge.'
    )

    parser.add_argument(
        '-diff_path', 
        type=str, 
        default='diff.patch', 
        help='Path to the diff file. Default is "diff.patch".'
    )

    parser.add_argument(
        '-minimal_coverage', 
        type=float, 
        default=0.75, 
        help='Minimal coverage percentage required. Default to 0.75.'
    )
    
    args = parser.parse_args()

    total_blocks, covered_blocks, coverage_percentage = merge_coverage_files('merged_coverage.out', *args.coverage_files)
    logging.info(f"Total modified blocks: {total_blocks}, Covered blocks: {covered_blocks}, Coverage: {coverage_percentage:.2f}%")

    # 调用主函数
    diff_path = args.diff_path  # 可以根据实际情况修改路径
    coverage_path = 'merged_coverage.out'  # 可以根据实际情况修改路径
    total_modified_lines, covered_modified_lines, coverage_percentage = diff_coverage(diff_path, coverage_path)
    logging.info(f"total_modified_lines: {total_modified_lines}, covered_modified_lines: {covered_modified_lines}, coverage_percentage:{coverage_percentage}")

    if coverage_percentage <= args.minimal_coverage:
        parse_file_coverage(args.minimal_coverage)
        logging.warning(f"The code coverage:{coverage_percentage} is below or equal {args.minimal_coverage}, not approved.")
        sys.exit(1)
    
    logging.info(f"The code coverage:{coverage_percentage} is above {args.minimal_coverage}, pass.")
