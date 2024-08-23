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
                        merged_coverage[key] = 1
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
    """解析diff文件，获取新增和修改的行号，仅处理 .go 文件"""
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
                    logging.info(f"Processing file: {current_file}")
                elif line.startswith('@@ ') and current_file:
                    # 解析 @@ 行，获取新文件的起始行号
                    match = re.match(r'@@ -\d+,\d+ \+(\d+),\d+ @@', line)
                    if match:
                        current_line_number = int(match.group(1))  # 新增代码块的起始行号
                        logging.info(f"New file starting line number: {current_line_number}")
                elif line.startswith('+') and current_file:
                    # 处理新增和修改的行
                    if current_line_number is not None:
                        if current_file not in modified_lines:
                            modified_lines[current_file] = []
                        modified_lines[current_file].append(current_line_number)
                        current_line_number += 1
                elif not line.startswith('-') and current_file:
                    # 处理未修改的行，递增行号
                    if current_line_number is not None:
                        current_line_number += 1
    except Exception as e:
        logging.error(f"Error parsing diff file: {e}")
        raise

    logging.info("Finished parsing diff file")
    return modified_lines

def parse_coverage_and_generate_report(coverage_path, modified_lines, output_path='pr_coverage.out'):
    """解析coverage.out文件，计算覆盖率并生成覆盖率报告"""
    logging.info(f"Starting to parse coverage file: {coverage_path}")
    total_modified_lines = 0
    covered_lines = set()
    seen_blocks = set()  # 用于跟踪已经处理过的代码块范围

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
                    executed_count = int(parts[1].split()[1])

                    # 处理行范围
                    line_range = line_info.split(',')
                    if len(line_range) != 2:
                        logging.warning(f"Unexpected line range format: {line_info}")
                        continue

                    try:
                        start_line = int(line_range[0].split('.')[0])
                        end_line = int(line_range[1].split('.')[0])
                    except ValueError as e:
                        logging.warning(f"Error parsing line range in coverage file: {e}")
                        continue

                    if (file_name, (start_line, end_line)) in seen_blocks:
                        continue

                    seen_blocks.add((file_name, (start_line, end_line)))

                    if file_name in modified_lines:
                        for modified_line in modified_lines[file_name]:
                            if start_line <= modified_line <= end_line:
                                if line not in covered_lines:
                                    total_modified_lines += 1
                                if executed_count > 0:
                                    covered_lines.add(line)
                                else:
                                    logging.info(f"[Not Covered line]{modified_line}")
                                pr_cov_file.write(line)
                                #break
                    
    except Exception as e:
        logging.error(f"Error parsing coverage file or generating report: {e}")
        raise

    coverage_percentage = (len(covered_lines) / total_modified_lines) if total_modified_lines > 0 else 0
    
    
    return total_modified_lines, len(covered_lines), coverage_percentage

def normalize_path(path, prefix='github.com/matrixorigin/matrixone/'):
    """移除特定前缀，规范化路径"""
    if path.startswith(prefix):
        return path[len(prefix):]
    return os.path.normpath(path)

def diff_coverage(diff_path, coverage_path, output_path='pr_coverage.out'):
    try:
        # 解析diff文件，获取修改和新增的行号
        modified_lines = parse_diff(diff_path)

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
        logging.warning(f"The code coverage:{coverage_percentage} is below {args.minimal_coverage}, not approved.")
        sys.exit(1)
    
    logging.info(f"The code coverage:{coverage_percentage} is above {args.minimal_coverage}, pass.")
