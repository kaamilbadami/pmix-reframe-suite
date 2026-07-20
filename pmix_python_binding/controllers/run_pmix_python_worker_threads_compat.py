import pmix
import subprocess
import sys
import os
import time
import random
import argparse
import datetime
import select

from pathlib import Path

import threading
import queue

from pmix_event_utils import (
    format_pmix_job_term_status,
    get_pmix_info_value,
)

# The fill_queue is the list of tasks to be run, but there are no slots availible to run them
fill_queue = []

# Messages is a list of messages emited by various log messages, either in the app or in spawned
# apps. TODO: Should this include the DVM?
messages = []

# list of all the times that was given to the sleeper apps
app_times = []

# List of IOF handlers. Currently unused other than adding handlers to this dict. Will likely be useful
handlers = {}

# Dict full of successfully spawned apps. Key is based on name space returned by DVM
running = {}

# Run condition variable that blocks the queuer thread until there are slots available to launch jobs
run_var = threading.Condition()

# Completion condition variable that blocks the main thread until completion
done_var = threading.Condition()

# Number of iterations completed. Main thread is awakened when completed == iters
completed = 0

# Number of tasks running
active = 0

parser = argparse.ArgumentParser(description='Control benchmark parameters.')
parser.add_argument('--slots', type=int, required=True)
parser.add_argument('--job-size', type=int, required=True)
parser.add_argument('--min-time', type=int, required=True)
parser.add_argument('--max-time', type=int, required=True)
parser.add_argument('--iters', type=int, required=True)
parser.add_argument('--out-file', type=str, required=True)
parser.add_argument('--delay', type=float, required=True)
parser.add_argument('--job', type=str, required=True)
parser.add_argument('--num-workers', type=int, required=False, default=1)


debug = True


def print_info(*args, **kwargs):
    fstring = str(args[0])
    rest = args[1:]
    info = kwargs.pop("info", None)
    if info is None:
        machine = "run_script"
    else:
        machine = info['nspace']

    new_string = "{} {} ".format(time.perf_counter(), machine) + fstring

    #  Remove 'flush' if it exists. We will control that var
    kwargs.pop("flush", None)

    if debug:
        print(new_string, *rest, **kwargs, flush=True)

    messages.append(new_string)


args = parser.parse_args()
print_info(args)

# Number of nodes - for accounting/verification purposes
num_nodes = args.slots
job_size = args.job_size
print("num_slots = {}".format(num_nodes))
print("job_size = {}".format(job_size))

# Number of cores in each node (default to 20)
num_cores_pn = 1 #${CI_NUM_CORES_PER_NODE:-20}

# Scale test based on number of nodes
ttl_num_cores = num_nodes * num_cores_pn

# Numer of worker threads
n_threads = args.num_workers

# Give each worker a queue so jobs can be dispatched deterministically. PMIx
# spawn blocks, but releases the GIL, so multiple dispatch threads are useful.
worker_queues = [queue.Queue() for _ in range(n_threads)]
used_worker_ids = set()
used_worker_ids_lock = threading.Lock()

# spawn invocations
num_iters = args.iters * ( ttl_num_cores // job_size )

# Aprox exstimate based on the average time waiting times the number of iterations
expected = ((args.min_time + args.max_time) / 2)*args.iters

print_info("Machine size: {} slots\nsize per job: {}\njob iterations: {}\nTotal spawns: {}\nTotal sleepers run: {}\nEstimated runtime: {}".format(ttl_num_cores, job_size, args.iters, num_iters, num_iters * job_size, str(datetime.timedelta(seconds=expected))), flush=True)

dvm_file = f"/tmp/{os.getpid()}.dvm.uri"

avail = ttl_num_cores


def add_task(app):
    fill_queue.append(app)
    return app['maxprocs']


def dispatch_task(app):
    worker_id = app['my_id'] % n_threads
    worker_queues[worker_id].put(app)


def select_tasks():
    global avail
    selected = []
    for task in fill_queue:
        if task['maxprocs'] <= avail:
            selected.append(task)
            avail -= task['maxprocs']
    for task in selected:
        fill_queue.remove(task)
    if debug:
        fill = sum(map(lambda x: x['maxprocs'], selected))
        print_info("Selected {} tasks in {} slots to dispatch. {} slots remain".format(len(selected), fill, avail))
    return selected


def mark_complete(source=None):
    global avail
    global completed
    global active

    print_info("Marking a job complete, source={}".format(source))

    with run_var:
        if source:
            active -= running[source['nspace']]['maxprocs']
            avail += running[source['nspace']]['maxprocs']
        run_var.notify()
    completed += 1
    if completed == num_iters:
        with done_var:
            done_var.notify()


#PMIX_EVENT_JOB_END
def next_handler(evhdlr:int, status:int,
                 source:dict, info:list, results:list):

    app = running[source['nspace']]
    term_status = get_pmix_info_value(
        info, pmix.PMIX_JOB_TERM_STATUS
    )
    if term_status is None:
        log_error('missing PMIX_JOB_TERM_STATUS', app)
    elif term_status != pmix.PMIX_SUCCESS:
        error_text = format_pmix_job_term_status(term_status)
        print_info(
            "Completion handler: job {} failed with {}".format(
                source['nspace'], error_text
            )
        )
        log_error(error_text, app)

    print_info(f"Completion handler: job {source['nspace']} {running[source['nspace']]['my_id']} has finished")
    print_info("Completion handler: Cleaning up {} slots from rank {}".format(running[source['nspace']]['maxprocs'], source['rank']))
    mark_complete(source)

    return pmix.PMIX_EVENT_ACTION_COMPLETE,None


def iof_cb(iofhdlr:int, channel:int,
           source:dict, payload:dict, info:list):
    messages = payload['bytes'][:int(payload['size'])].decode('UTF-8').strip()
    for message in messages.split("\n"):
        print_info(message, info=source)

errors = dict()
def log_error(rc,app):
    if rc in errors:
        errors[rc]['count'] += 1
        errors[rc]['jobs'].append(app)
    else:
        errors[rc] = dict()
        errors[rc]['count'] = 1
        errors[rc]['jobs'] = [ app ]

def shutdown_dvm():
    # ---------------------------------------
    # Cleanup DVM
    # ---------------------------------------
    subprocess.run([
        str(Path(os.environ["PRRTE"]) / "bin" / "pterm"),
        "--dvm-uri",
        "file:{}".format(dvm_file),
        "--num-connect-retries",
        "1000"
    ])

sleeper = Path(args.job).resolve()

if not sleeper.exists():
    print("ERROR: Missing executable './sleeper'\n INFO: Remember to run './build.sh' first.\n\tGiven path was: '{}'".format(str(sleeper)))
    print("Continuing")

dvm_messages = []

def add_to_dvm_msg(fileh, name):
    msg = fileh.readline().decode('utf8').strip()
    msg_list = msg.split("\n")
    for line in msg_list:
        dvm_messages.append("{}: {}".format(name, line))

def prte_log_thread():
    global dvm

    while True:
        ready = select.select([dvm.stdout, dvm.stderr], [], [])

        if dvm.stdout in ready[0]:
            add_to_dvm_msg(dvm.stdout, "stdout")
        if dvm.stderr in ready[0]:
            add_to_dvm_msg(dvm.stderr, "stderr")

prte_prefix = os.environ["PRRTE"]
prte_args = [f"{prte_prefix}/bin/prte", "--prefix", prte_prefix, "--report-uri", dvm_file, "--prtemca", "plm", "^slurm,lsf", "--prtemca", "ras", "^slurm,lsf"]
if "CI_HOSTFILE" in os.environ:
    print_info("Using hostfile: {}".format(os.environ["CI_HOSTFILE"]))
    prte_args.extend(["--hostfile", os.environ["CI_HOSTFILE"]])

print_info("Launching prte with args: {}".format(", ".join(prte_args)))

#Popen is nonblocking, so no shell games needed, the script just owns prte
dvm = subprocess.Popen(prte_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
while True:
    outs = dvm.stdout.readline().decode('utf8').strip()
    if outs:
        print_info("dvm output: '{}'".format(outs))
    if outs == "DVM ready":
        break
    if outs == "":
        err = dvm.stderr.readline().decode('utf8').strip()
        raise RuntimeError("PRTE exited before readiness. stderr: {}".format(err))

threading.Thread(target=prte_log_thread, daemon=True).start()

def worker(worker_id):
    info = [{'key':pmix.PMIX_MAPBY, 'value':"hwthread:NOLOCAL", 'val_type':pmix.PMIX_STRING},
            {'key':pmix.PMIX_BINDTO, 'value':"hwthread", 'val_type':pmix.PMIX_STRING},
            {'key':pmix.PMIX_NOTIFY_COMPLETION, 'value':True, 'val_type':pmix.PMIX_BOOL}]
    sleep_time = args.delay
    while True:
        global active
        if sleep_time:
            time.sleep(sleep_time)
        app = worker_queues[worker_id].get()
        print_info("Worker {} about to launch task {}".format(worker_id, app))
        rc, nspace = tool.spawn(info, [app])
        print_info("New task: {}".format(nspace))
        if rc != pmix.PMIX_SUCCESS:
            global avail
            print_info("Spawn Oops", tool.error_string(rc))
            print_info("Job that oopsed: {} avail: {} active: {}".format(app, avail, active), flush=True)
            # These two lines are to enable a fail semantic.
            avail += app['maxprocs']
            log_error(tool.error_string(rc), app)
            mark_complete()
            continue

        with used_worker_ids_lock:
            used_worker_ids.add(worker_id)

        with run_var:
            running[nspace] = app
            active += app['maxprocs']

        rc, id = tool.iof_pull([{'nspace': nspace, 'rank': pmix.PMIX_RANK_WILDCARD}], pmix.PMIX_FWD_STDOUT_CHANNEL | pmix.PMIX_FWD_STDERR_CHANNEL, [], iof_cb)
        handlers[nspace] = id

def queuer():
    while True:
        with run_var:
            run_var.wait()
            apps = select_tasks()
            if apps:
                print_info(apps)
            for app in apps:
                dispatch_task(app)

threading.Thread(target=queuer, daemon=True).start()
for worker_id in range(n_threads):
    threading.Thread(target=worker, args=(worker_id,), daemon=True).start()
tool = pmix.PMIxTool()
rc,my_proc = tool.init([{"key": pmix.PMIX_SERVER_URI, "value": "file:{}".format(dvm_file), 'val_type':pmix.PMIX_STRING}])
rc,myhandle = tool.register_event_handler([pmix.PMIX_EVENT_JOB_END], [], next_handler)

print_info("generating tasks")
random.seed(time.time())
for idx in range(num_iters):
    num_seconds = random.randint(args.min_time, args.max_time)
    app = {'cmd':str(sleeper), 'argv':[str(sleeper),"-n",str(num_seconds), "-i", str(idx)], 'maxprocs':job_size, 'my_id': idx}
    app_times.append(str(num_seconds))
    add_task(app)

print_info("Starting timer")
start = time.perf_counter()
apps = select_tasks()
for app in apps:
    dispatch_task(app)
try:
    with done_var:
        done_var.wait()
except KeyboardInterrupt:
    print_info("killing {} tasks".format(len(fill_queue)))
finally:
    end = time.perf_counter()
    print_info("Main thread awakens")
    time_taken = end - start
    print_info("Elapsed seconds: {}".format(time_taken))
    print_info("Elapsed time: {}".format(str(datetime.timedelta(seconds=time_taken))))
    print_info("Overhead seconds: {}".format(time_taken - expected))

    if errors:
        print_info("Some jobs failed\nPMIx error: count")
        for i in errors:
            print_info("{}: {}".format(i, errors[i]))

    # This extrea string at the end is a hack to make sure that the file ends with a \n.
    messages.append("")
    with open(args.out_file, 'w') as out_file:
        out_file.write("\n".join(messages))

    # Write list of times each sleeper app spent sleeping
    app_times.append("")
    with open(args.out_file+".sleep_times", 'w') as out_file:
        out_file.write("\n".join(app_times))

    dvm_messages.append("")
    with open(args.out_file+".dvm_output", 'w') as out_file:
        out_file.write("\n".join(dvm_messages))

    # do sanity tests here
    shutdown_dvm()
    expected_worker_ids = set(range(n_threads))
    worker_coverage_failed = used_worker_ids != expected_worker_ids
    if worker_coverage_failed:
        print_info(
            "Worker coverage mismatch: used {}; expected {}".format(
                sorted(used_worker_ids), sorted(expected_worker_ids)
            )
        )

    if errors or completed != num_iters or worker_coverage_failed:
        print_info(
            "WORKER COUNT {} FAIL: completed={} expected={} errors={}".format(
                n_threads, completed, num_iters, len(errors)
            )
        )
        sys.exit(1)

    print("workers used: {}".format(sorted(used_worker_ids)), flush=True)
    print_info("WORKER COUNT {} PASS".format(n_threads))
