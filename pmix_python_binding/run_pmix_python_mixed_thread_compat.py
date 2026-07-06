import pmix
import subprocess
import sys
import os
import time
import random
import time
import argparse
import datetime
import select
import math

from pathlib import Path
from io import StringIO

import threading
import queue

# Worker thread blocks on run_queue. PMIx spawn blocks on spawn, but releases the GIL.
# This means that multiple dispatch threads will be benifitial. This may change if non-blocking
# spawns are implemented
run_queue = queue.Queue()

# The fill_queue is the list of tasks to be run, but there are no slots availible to run them
fill_queue = []

# Messages is a list of messages emited by various log messages, either in the app or in spawned
# apps. TODO: Should this include the DVM?
messages = []

# list of all the times that was given to the sleeper apps
app_times = []
job_sizes = []
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
parser.add_argument('--job-size-min', type=int, required=True)
parser.add_argument('--job-size-max', type=int, required=True)
parser.add_argument('--min-time', type=int, required=True)
parser.add_argument('--max-time', type=int, required=True)
parser.add_argument('--iters', type=int, required=True)
parser.add_argument('--out-file', type=str, required=True)
parser.add_argument('--delay', type=float, required=True)
parser.add_argument('--job', type=str, required=True)
parser.add_argument('--seed', type=int, required=False)


debug = True


def print_info(*args, **kwargs):
    global out_file
    fstring = str(args[0])
    rest = args[1:]
    info = kwargs.pop("info", None)
    if info is None:
        machine = "run_script"
    else:
        machine = info['nspace']

    #new_string = "{} {} ".format(time.mktime(datetime.datetime.today().timetuple()), machine) + fstring
    new_string = "{} {} ".format(time.perf_counter(), machine) + fstring + "\n"

    #  Remove 'flush' if it exists. We will control that var
    kwargs.pop("flush", None)

    if debug:
        print(new_string, *rest, **kwargs, flush=True)

    #messages.append(new_string)
    out_file.write(new_string)


args = parser.parse_args()
out_file = open(args.out_file, 'w')
print_info(args)

# Number of nodes - for accounting/verification purposes
num_nodes = args.slots
job_size_min = args.job_size_min
job_size_max = args.job_size_max

print("num_slots = {}".format(num_nodes))
print("job_size_min = {}".format(job_size_min))
print("job_size_max = {}".format(job_size_max))

# Number of cores in each node (default to 20)
num_cores_pn = 1 #${CI_NUM_CORES_PER_NODE:-20}

# Scale test based on number of nodes
ttl_num_cores = num_nodes * num_cores_pn

# Enable more verbose output (set VERBOSE=1)
verbose = 1

# TJN: Stash some debug bits for now
#debug = True

# Numer of worker threads
n_threads = 1

# spawn invocations
avg_job_size = int(math.ceil((job_size_min + job_size_max)/2.0))
num_iters = args.iters * ( ttl_num_cores // avg_job_size )

# Aprox exstimate based on the average time waiting times the number of iterations
expected = ((args.min_time + args.max_time) / 2)*args.iters

print_info("Machine size: {} slots\nsize per job: {}-{}\njob iterations: {}\nTotal spawns: {}\nTotal sleepers run: {}\nEstimated runtime: {}".format(ttl_num_cores, job_size_min, job_size_max, args.iters, num_iters, num_iters * (job_size_min + job_size_max)/2.0, str(datetime.timedelta(seconds=expected))), flush=True)

#dvm_file = "/tmp/bzfdvm.uri"
dvm_file = f"/tmp/{os.getpid()}.dvm.uri"

slots = ttl_num_cores
avail = ttl_num_cores
used = 0


def add_task(app):
    fill_queue.append(app)
    return app['maxprocs']


def select_tasks():
    global avail
    selected = []
    #print_info(f"SELECT_TASK() slots available = {avail} " )
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


def give_back(app):
    global avail
    in_avail = sum([add_task(x) for x in app])
    print_info("Giving back {} slots due to errors".format(in_avail))
    avail += in_avail
    with run_var:
        run_var.notify()


def mark_complete(source=None):
    global avail
    global completed
    global active

    print_info("Marking a job complete, source={}".format(source))

    with run_var:
        #print_info("AQUIRED RUN_VAR")
        #print_info(f"AQUIRED RUN_VAR {source}")
        if source:
            active -= running[source['nspace']]['maxprocs']
            avail += running[source['nspace']]['maxprocs']
        #print_info(f"RUN_VAR NOTIFYING")
        #print_info(f"RUN_VAR NOTIFYING   {avail} {active}")
        run_var.notify()
    completed += 1
    if completed == num_iters:
        with done_var:
            done_var.notify()


#PMIX_EVENT_JOB_END
def next_handler(evhdlr:int, status:int,
                 source:dict, info:list, results:list):
    global active

    #print("next_handler FIRED")
    job = running[source['nspace']]
    #print(f"FIRED {job}")
    nslots = int(job['maxprocs'])
    #print(f"FIRED {nslots}")
    util = active - nslots
    #print(f"FIRED job {job}  nslots {nslots} util {util}")
    print_info(f"Completion handler: job {job} has finished  TOTAL UTIL {util}")
    #print_info(f"FIRED source  = {source}")
    print_info("Completion handler: Cleaning up {} slots from rank {}".format(running[source['nspace']]['maxprocs'], source['rank']))
    mark_complete(source)

    return pmix.PMIX_EVENT_ACTION_COMPLETE,None


def iof_cb(iofhdlr:int, channel:int,
           source:dict, payload:dict, info:list):
    messages = payload['bytes'][:int(payload['size'])].decode('UTF-8').strip()
    for message in messages.split("\n"):
        print_info(message, info=source)
    #messages.append(message)

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
    # Todo: See if we can empty stdout/error. The below will block if empty
    # print(dvm.stdout.read().decode('utf8').strip())
    subprocess.run([
        str(Path(os.environ["PRRTE"]) / "bin" / "pterm"),
        "--dvm-uri",
        "file:{}".format(dvm_file),
        "--num-connect-retries",
        "1000"
    ])

#sleeper = Path("/autofs/nccs-svm1_home1/bzf/pmixpy/run-test/sleeper_mpi").resolve()
sleeper = Path(args.job).resolve()

if not sleeper.exists():
    print("ERROR: Missing executable './sleeper'\n INFO: Remember to run './build.sh' first.\n\tGiven path was: '{}'".format(str(sleeper)))
    print("Continuing")
    # sys.exit(1)

dvm_messages = []

# TODO: Factoring the DVM code into a function maybe good hygine

def add_to_dvm_msg(fileh, name):
    msg = fileh.readline().decode('utf8').strip()
    msg_list = msg.split("\n")
    for line in msg_list:
        dvm_messages.append("{}: {}".format(name, line))

def prte_log_thread():
    global dvm

    while True:
        outs = ""
        errs = ""
        ready = select.select([dvm.stdout, dvm.stderr], [], [])

        if dvm.stdout in ready[0]:
            add_to_dvm_msg(dvm.stdout, "stdout")
        if dvm.stderr in ready[0]:
            add_to_dvm_msg(dvm.stderr, "stderr")

#prte_args = ["prte", "--report-uri", "dvm.uri"]
#spock requires some extra args
#prte_args = ["prte", "--report-uri", dvm_file, "--prtemca", "plm", "^slurm,lsf", "--prtemca", "ras", "^slurm,lsf", "--map-by", ":NOLOCAL", "--pmixmca", "pmix_server_spawn_verbose", "10"]
#prte_args = ["prte", "--report-uri", dvm_file, "--prtemca", "plm", "^slurm,lsf", "--prtemca", "ras", "^slurm,lsf", "--prtemca", "rmaps_base_verbose", "5"]
prte_prefix = os.environ["PRRTE"]
prte_args = [f"{prte_prefix}/bin/prte", "--prefix", prte_prefix, "--report-uri", dvm_file, "--prtemca", "plm", "^slurm,lsf", "--prtemca", "ras", "^slurm,lsf"]
#time.sleep(10)
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

def worker():
    #sleep_time = 0
    # {'key':pmix.PMIX_NO_PROCS_ON_HEAD, 'value':True, 'val_type':pmix.PMIX_BOOL}
    info = [{'key':pmix.PMIX_MAPBY, 'value':"hwthread:NOLOCAL", 'val_type':pmix.PMIX_STRING},
            {'key':pmix.PMIX_BINDTO, 'value':"hwthread", 'val_type':pmix.PMIX_STRING}]
#            {'key':pmix.PMIX_IOF_TAG_OUTPUT, 'value':True, 'val_type':pmix.PMIX_BOOL},
#            {'key':pmix.PMIX_IOF_TIMESTAMP_OUTPUT, 'value':True, 'val_type':pmix.PMIX_BOOL},
#            {'key':pmix.PMIX_IOF_OUTPUT_TO_FILE, 'value':'dummy.outfile', 'val_type':pmix.PMIX_STRING}]
    sleep_time = args.delay
    while True:
        global active
        if sleep_time:
            time.sleep(sleep_time)
        app = run_queue.get()
        print_info("About to launch task {}".format(app))
        rc, nspace = tool.spawn(info, [app])
        active += app['maxprocs']
        print_info(f"New task: {nspace} TOTAL_UTIL = {active}")
        if rc != pmix.PMIX_SUCCESS:
            global avail
            print_info("Spawn Oops", tool.error_string(rc))
            print_info("Job that oopsed: {} avail: {} active: {}".format(app, avail, active), flush=True)
            # At present a failure to spawn is fatal to the job. The commented out function below is meant to allow for a
            # retry mechanism. Future version of the script should allow for a choice between retry vs fail semantics.
            # The retry mechanism probably works, but it is untested.
            # give_back([app])

            # These two lines are to enable a fail semantic.
            avail += app['maxprocs']
            active -= app['maxprocs']
            mark_complete()

            # Error should be logged both ways
            log_error(tool.error_string(rc), app)
            continue

        running[nspace] = app
        rc, id = tool.iof_pull([{'nspace': nspace, 'rank': pmix.PMIX_RANK_WILDCARD}], pmix.PMIX_FWD_STDOUT_CHANNEL | pmix.PMIX_FWD_STDERR_CHANNEL, [], iof_cb)
        handlers[nspace] = id

def queuer():
    while True:
        with run_var:
            #print("QUEUER WAITING")
            run_var.wait()
            #print_info("QUEUER WAKE UP")
            apps = select_tasks()
            if apps:
                print_info(apps)
            for app in apps:
                run_queue.put(app)

threading.Thread(target=queuer, daemon=True).start()
for _ in range(n_threads):
    threading.Thread(target=worker, daemon=True).start()
#TODO: write stdout to a file for sanity tests
tool = pmix.PMIxTool()
rc,my_proc = tool.init([{"key": pmix.PMIX_SERVER_URI, "value": "file:{}".format(dvm_file), 'val_type':pmix.PMIX_STRING}])
rc,myhandle = tool.register_event_handler([pmix.PMIX_EVENT_JOB_END], [], next_handler)

print_info("generating tasks")
if args.seed is not None:
    random.seed(args.seed)
else:
    random.seed(time.time())
total_sec = 0
for idx in range(num_iters):
    #maxprocs = random.randint(1,8)
    num_seconds = random.randint(args.min_time, args.max_time)
    job_size = random.randint(args.job_size_min, args.job_size_max)
    total_sec += num_seconds
    app = {'cmd':str(sleeper), 'argv':[str(sleeper),"-n",str(num_seconds)], 'maxprocs':job_size, 'my_id': idx}
    print_info(f"ADDED TASK TO QUEUE {app}")
    app_times.append(str(num_seconds))
    job_sizes.append(str(job_size))
    add_task(app)

print_info("Starting timer")
start = time.perf_counter()
apps = select_tasks()
for app in apps:
    run_queue.put(app)
try:
    with done_var:
        done_var.wait()
except KeyboardInterrupt:
    print_info("killing {} tasks".format(len(fill_queue)))
finally:
    end = time.perf_counter()
    print_info("Main thread awakens")
    time_taken = end - start
    #print("Elapsed seconds: {}".format(str(datetime.timedelta(seconds=time_taken))))
    #print("Overhead: {}".format(str(datetime.timedelta(seconds=time_taken - expected))))
    print_info("Elapsed seconds: {}".format(time_taken))
    print_info("Elapsed time: {}".format(str(datetime.timedelta(seconds=time_taken))))
    print_info("Overhead seconds: {}".format(time_taken - expected))

    if errors:
        print_info("Some jobs failed\nPMIx error: count")
        for i in errors:
            print_info("{}: {}".format(i, errors[i]))

    # This extrea string at the end is a hack to make sure that the file ends with a \n.
    #messages.append("")
    #with open(args.out_file, 'w') as out_file:
    #    out_file.write("\n".join(messages))

    # Write list of times each sleeper app spent sleeping
    app_times.append("")
    with open(args.out_file+".sleep_times", 'w') as sleep_times_file:
        sleep_times_file.write("\n".join(app_times))

    job_sizes.append("")
    with open(args.out_file+".job_sizes", 'w') as job_sizes_file:
        job_sizes_file.write("\n".join(job_sizes))

    dvm_messages.append("")
    with open(args.out_file+".dvm_output", 'w') as dvm_output_file:
        dvm_output_file.write("\n".join(dvm_messages))

    # do sanity tests here
    shutdown_dvm()
    if errors or completed != num_iters:
        print_info(
            "MIXED JOB SIZES FAIL: completed={} expected={} errors={}".format(
                completed, num_iters, len(errors)
            )
        )
        out_file.close()
        sys.exit(1)

    print_info("MIXED JOB SIZES PASS")
    out_file.close()
