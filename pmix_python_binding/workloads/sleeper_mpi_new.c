/*
 * Usage:  sleeper NSECONDS [LAUNCHID]
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <mpi.h>

#define NUM_SEC   1  /* default number of seconds */
#define ID_NUM    1  /* default launch id number */

#ifndef PMIX_TEST_EXIT_CODE
#define PMIX_TEST_EXIT_CODE 0
#endif

void usage(void)
{
    char *name = "sleeper";

    printf("Usage: %s  [-n SECONDS]  [-i NUMBER]\n", name);
    printf("  -n SECONDS    Number of seconds (postive integer, default: %d)\n", NUM_SEC);
    printf("  -i NUMBER     Identifier number (positive integer, default: %d)\n", ID_NUM);
    return;
}

int main(int argc, char **argv)
{
    char host[128];
    int rc;
    int nsec = NUM_SEC;
    int launchid = ID_NUM;
    pid_t pid = getpid();
    int opt, mpi_rank, mpi_size;

    MPI_Init(&argc, &argv);

    while ((opt = getopt(argc, argv, "hn:i:")) != -1) {
        switch (opt) {
        case 'n':
            /* Number of seconds */
            nsec = atoi(optarg);
            break;
        case 'i':
            /* Identifier number*/
            launchid = atoi(optarg);
            break;
        case 'h':
            /* Show help and exit */
            usage();
            exit (EXIT_SUCCESS);
        }
    }

    if (0 > (rc = gethostname(host, sizeof(host)))) {
        fprintf(stderr, "(%6d) Error: failed to obtain hostname (rc=%d)\n", pid, rc);
        return (1);
    }

    MPI_Comm_rank(MPI_COMM_WORLD, &mpi_rank);
    MPI_Comm_size(MPI_COMM_WORLD, &mpi_size);

    sleep(nsec);

    fprintf(stdout, "(%06d.%6d) [%s] %2d %2d  DONE (slept %d seconds)\n", launchid, pid, host, mpi_rank, mpi_size, nsec);

    if (0 != PMIX_TEST_EXIT_CODE) {
        fprintf(stderr, "INTENTIONAL PMIX PAYLOAD FAILURE: exit_code=%d\n",
                PMIX_TEST_EXIT_CODE);
    }

    MPI_Finalize();

    return PMIX_TEST_EXIT_CODE;
}
