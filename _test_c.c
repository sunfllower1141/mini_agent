#include <stdio.h>
#include <stdlib.h>

#define MAX_SIZE 100
#define SQUARE(x) ((x) * (x))

typedef struct Point {
    int x;
    int y;
} Point;

struct Rect {
    int width;
    int height;
};

union Data {
    int i;
    float f;
};

enum Color {
    RED,
    GREEN,
    BLUE
};

// Forward declaration
int compute_area(int w, int h);

// Function definition
int compute_area(int w, int h) {
    int result = w * h;
    return result;
}

static void helper_func(void) {
    printf("helper\n");
}

int main(int argc, char *argv[]) {
    int a = compute_area(10, 20);
    helper_func();
    return 0;
}
