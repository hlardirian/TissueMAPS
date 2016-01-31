import os


def get_module_directories(repo_dir):
    '''
    Get the directories were module source code files are located.

    Parameters
    ----------
    repo_dir: str
        value of the "lib" key in the pipeline descriptor file

    Returns
    -------
    Dict[str, str]
        paths to module directories for each language relative to the
        repository directory
    '''
    dirs = {
        'Python': 'python/jtlib/modules',
        'Matlab': 'matlab/+jtlib/+modules',
        'R': 'r/jtlib/modules',  # TODO: package layout in R???
    }
    return {k: os.path.join(repo_dir, v) for k, v in dirs.iteritems()}


def complete_path(input_path, project_dir):
    '''
    Complete relative path variables.

    Parameters
    ----------
    input_path: str
        relative path the should be completed
    project_dir: str
        absolute path to project folder

    Returns
    -------
    str
        absolute path
    '''
    if not input_path:
        return input_path
    else:
        input_path = os.path.expandvars(input_path)
        input_path = os.path.expanduser(input_path)
        if input_path.startswith('.'):
            input_path = os.path.join(project_dir, input_path)
        return input_path


def get_module_path(module_file, repo_dir):
    '''
    Get absolute path to module file.
    Parameters
    ----------
    module_file: str
        name of the module file
    repo_dir: str
        absolute path to the local copy of the `jtlib` repository

    Returns
    -------
    str
        absolute path to module file
    '''
    language = determine_language(module_file)
    modules_dir = get_module_directories(repo_dir)[language]
    return os.path.join(modules_dir, module_file)


def determine_language(filename):
    '''
    Determine language form module filename suffix.

    Parameters
    ----------
    filename: str
        name of a module file

    Returns
    -------
    str
    '''
    filename = os.path.abspath(filename)  # removes trailing '/'
    suffix = os.path.splitext(os.path.basename(filename))[1]
    if suffix == '.m':
        return 'Matlab'
    elif suffix == '.R' or suffix == '.r':
        return 'R'
    elif suffix == '.py':
        return 'Python'
    elif suffix == '.jl':
        return 'Julia'
    else:
        raise Exception('Language could not be determined from filename.')
